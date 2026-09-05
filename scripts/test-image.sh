#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 IMAGE PLATFORM EXPECTED_ARCH" >&2
  exit 2
fi

image=$1
platform=$2
expected_arch=$3

docker run --rm \
  --platform "$platform" \
  --env PUID=1234 \
  --env PGID=1234 \
  "$image" \
  /bin/sh -ec '
    expected_arch=$1
    test "$(uname -m)" = "$expected_arch"
    test "$(id -u abc)" = 1234
    test "$(id -g abc)" = 1234
    test "$(stat -c %u /app)" = 1234
    test "$(stat -c %g /config)" = 1234
    command -v curl >/dev/null
    command -v jq >/dev/null
    command -v python3 >/dev/null
    command -v unrar >/dev/null
    unrar | grep -q "UNRAR"
  ' smoke "$expected_arch"
