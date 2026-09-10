#!/bin/sh
set -eu

die() {
    printf 'apk-lock: %s\n' "$*" >&2
    exit 1
}

new_temporary_file() {
    temporary_file=$(mktemp)
    temporary_files="${temporary_files:-} ${temporary_file}"
}

cleanup() {
    for temporary_file in ${temporary_files:-}; do
        rm -f "$temporary_file"
    done
}

trap cleanup EXIT HUP INT TERM

inventory() {
    new_temporary_file
    manifest_file=$temporary_file
    apk list --installed --manifest > "$manifest_file"
    new_temporary_file
    exact_inventory=$temporary_file
    awk '
        NF == 2 && $1 ~ /^[A-Za-z0-9][A-Za-z0-9+_.-]*$/ &&
            $2 ~ /^[A-Za-z0-9][A-Za-z0-9+_.:~-]*$/ {
            print $1 "=" $2
            next
        }
        {
            printf "apk-lock: malformed installed package manifest row: %s\n", $0 > "/dev/stderr"
            exit 1
        }
    ' "$manifest_file" > "$exact_inventory" || exit 1
    LC_ALL=C sort -t '=' -k 1,1 "$exact_inventory"
}

normalize_requests() {
    requests_file=$1
    output_file=$2
    [ -f "$requests_file" ] || die "request file does not exist: $requests_file"
    new_temporary_file
    unsorted_requests=$temporary_file
    awk '
        {
            sub(/#.*/, "")
            gsub(/^[[:space:]]+|[[:space:]]+$/, "")
            if ($0 == "") next
            if ($0 !~ /^[A-Za-z0-9][A-Za-z0-9+_.-]*$/) {
                printf "apk-lock: invalid unversioned package request: %s\n", $0 > "/dev/stderr"
                exit 1
            }
            if (!seen[$0]++) print
        }
    ' "$requests_file" > "$unsorted_requests" || exit 1
    LC_ALL=C sort "$unsorted_requests" > "$output_file"
    [ -s "$output_file" ] || die "package request list is empty"
}

validate_lock() {
    lock_file=$1
    package_file=$2
    [ -f "$lock_file" ] || die "lock file does not exist: $lock_file"

    line_one=$(sed -n '1p' "$lock_file")
    architecture_line=$(sed -n '2p' "$lock_file")
    parent_line=$(sed -n '3p' "$lock_file")
    requests_line=$(sed -n '4p' "$lock_file")
    [ "$line_one" = "# apk-lock: 1" ] || die "unsupported or malformed lock format"

    case "$architecture_line" in
        '# architecture: x86_64'|'# architecture: aarch64'|'# architecture: armv7') ;;
        *) die "unsupported or malformed lock architecture" ;;
    esac
    lock_architecture=${architecture_line#\# architecture: }

    lock_parent=${parent_line#\# parent: }
    [ "$parent_line" = "# parent: $lock_parent" ] || die "malformed lock parent"
    printf '%s\n' "$lock_parent" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$' ||
        die "lock parent must be pinned by SHA-256 digest"

    requests_sha256=${requests_line#\# requests-sha256: }
    [ "$requests_line" = "# requests-sha256: $requests_sha256" ] ||
        die "malformed request-list digest"
    printf '%s\n' "$requests_sha256" | grep -Eq '^[0-9a-f]{64}$' ||
        die "invalid request-list SHA-256 digest"

    [ -z "$(tail -c 1 "$lock_file")" ] || die "lock file must end with a newline"
    tail -n +5 "$lock_file" > "$package_file"
    [ -s "$package_file" ] || die "lock package inventory is empty"

    awk '
        /^[A-Za-z0-9][A-Za-z0-9+_.-]*=[A-Za-z0-9][A-Za-z0-9+_.:~-]*$/ {
            separator = index($0, "=")
            name = substr($0, 1, separator - 1)
            if (seen[name]++) {
                printf "apk-lock: duplicate locked package: %s\n", name > "/dev/stderr"
                exit 1
            }
            next
        }
        {
            printf "apk-lock: malformed exact package constraint: %s\n", $0 > "/dev/stderr"
            exit 1
        }
    ' "$package_file" || exit 1

    new_temporary_file
    sorted_packages=$temporary_file
    LC_ALL=C sort -t '=' -k 1,1 "$package_file" > "$sorted_packages"
    cmp -s "$package_file" "$sorted_packages" || die "lock package inventory is not canonical"

    while IFS='=' read -r package_name package_version; do
        if ! apk version --check "$package_version" >/dev/null; then
            die "unsupported package version for $package_name: $package_version"
        fi
    done < "$package_file"
}

require_lock_architecture() {
    installed_architecture=$(apk --print-arch)
    [ "$installed_architecture" = "$lock_architecture" ] ||
        die "lock architecture $lock_architecture does not match installed architecture $installed_architecture"
}

add_from_files() {
    new_temporary_file
    all_constraints=$temporary_file
    : > "$all_constraints"
    for constraint_file do
        cat "$constraint_file" >> "$all_constraints"
    done
    set --
    while IFS= read -r constraint; do
        set -- "$@" "$constraint"
    done < "$all_constraints"
    apk add --no-cache "$@"
}

verify_lock() {
    lock_file=$1
    new_temporary_file
    locked_packages=$temporary_file
    validate_lock "$lock_file" "$locked_packages"
    require_lock_architecture

    new_temporary_file
    installed_packages=$temporary_file
    inventory > "$installed_packages"
    if ! cmp -s "$locked_packages" "$installed_packages"; then
        printf 'apk-lock: installed package inventory does not match %s\n' "$lock_file" >&2
        diff -u "$locked_packages" "$installed_packages" >&2 || true
        return 1
    fi
}

resolve() {
    requests_file=$1
    mode=$2
    new_temporary_file
    normalized_requests=$temporary_file
    normalize_requests "$requests_file" "$normalized_requests"

    case "$mode" in
        base)
            apk upgrade --no-cache >&2
            set --
            while IFS= read -r package_name; do
                set -- "$@" "$package_name"
            done < "$normalized_requests"
            apk add --no-cache --upgrade "$@" >&2
            ;;
        inherited)
            new_temporary_file
            inherited_inventory=$temporary_file
            inventory > "$inherited_inventory"

            new_temporary_file
            inherited_requests=$temporary_file
            awk -F= '
                NR == FNR { installed[$1] = 1; next }
                !($1 in installed) { print }
            ' "$inherited_inventory" "$normalized_requests" > "$inherited_requests"

            new_temporary_file
            resolver_error=$temporary_file
            if ! add_from_files "$inherited_inventory" "$inherited_requests" > "$resolver_error" 2>&1; then
                resolver_detail=$(tr '\n' ' ' < "$resolver_error" | sed 's/[[:space:]]*$//')
                if grep -Eqi 'temporary error|unable to fetch|network|connection|timed out|timeout|tls|certificate|unauthorized|authentication|permission denied|(^|[^0-9])(401|403)([^0-9]|$)' "$resolver_error"; then
                    printf 'apk-lock: failed to resolve inherited packages: %s\n' \
                        "$resolver_detail" >&2
                elif grep -Eqi 'breaks:|conflicts:' "$resolver_error" &&
                    grep -Eq 'world\[[A-Za-z0-9][A-Za-z0-9+_.-]*=[A-Za-z0-9][A-Za-z0-9+_.:~-]*\]' "$resolver_error"; then
                    printf 'apk-lock: inherited package constraints conflict: %s\n' \
                        "$resolver_detail" >&2
                else
                    printf 'apk-lock: failed to resolve inherited packages: %s\n' \
                        "$resolver_detail" >&2
                fi
                return 1
            fi

            new_temporary_file
            resolved_inventory=$temporary_file
            inventory > "$resolved_inventory"
            if ! awk -F= '
                NR == FNR { inherited[$1] = $2; next }
                $1 in inherited && inherited[$1] == $2 { delete inherited[$1] }
                END { exit(length(inherited) == 0 ? 0 : 1) }
            ' "$inherited_inventory" "$resolved_inventory"; then
                die "inherited package constraints changed during resolution"
            fi
            ;;
        *) die "resolve mode must be base or inherited" ;;
    esac

    inventory
}

install_lock() {
    lock_file=$1
    mode=${2:-base}
    case "$mode" in
        base|inherited) ;;
        *) die "install mode must be base or inherited" ;;
    esac
    new_temporary_file
    locked_packages=$temporary_file
    validate_lock "$lock_file" "$locked_packages"
    require_lock_architecture
    if [ "$mode" = inherited ]; then
        new_temporary_file
        parent_inventory=$temporary_file
        inventory > "$parent_inventory"
        if ! awk -F= '
            NR == FNR { locked[$1] = $2; next }
            !($1 in locked) {
                printf "apk-lock: inherited package missing from lock: %s=%s\n", $1, $2 > "/dev/stderr"
                failed = 1
                next
            }
            locked[$1] != $2 {
                printf "apk-lock: inherited package version changed in lock: %s=%s (locked %s)\n", $1, $2, locked[$1] > "/dev/stderr"
                failed = 1
            }
            END { exit(failed ? 1 : 0) }
        ' "$locked_packages" "$parent_inventory"; then
            return 1
        fi
    fi
    add_from_files "$locked_packages" >&2
    verify_lock "$lock_file"
}

command=${1:-}
case "$command" in
    inventory)
        [ "$#" -eq 1 ] || die "usage: apk-lock inventory"
        inventory
        ;;
    resolve)
        [ "$#" -eq 3 ] || die "usage: apk-lock resolve REQUESTS_FILE base|inherited"
        resolve "$2" "$3"
        ;;
    install)
        if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
            die "usage: apk-lock install LOCK_FILE [base|inherited]"
        fi
        install_lock "$2" "${3:-base}"
        ;;
    verify)
        [ "$#" -eq 2 ] || die "usage: apk-lock verify LOCK_FILE"
        verify_lock "$2"
        ;;
    *) die "usage: apk-lock inventory|resolve|install|verify" ;;
esac
