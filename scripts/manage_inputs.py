#!/usr/bin/env python3
"""Resolve, verify, and report immutable base-image package inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Mapping

try:
    from scripts import build_report, package_locks
except ImportError:  # Direct execution places scripts/ on sys.path.
    import build_report  # type: ignore[no-redef]
    import package_locks  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "saltydk/alpine-s6overlay:latest"
REPOSITORY = "saltydk/docker-alpine-s6overlay"
PLATFORMS = tuple(package_locks.ARCHITECTURES)
PROFILES = ("builder", "runtime")
PARENT_PATTERN = re.compile(r"alpine:[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}\Z")
PARENT_ARG_PATTERN = re.compile(
    r"^ARG\s+ALPINE_IMAGE=(alpine:[0-9]+\.[0-9]+@sha256:[0-9a-f]{64})\s*$", re.MULTILINE
)
FROM_PATTERN = re.compile(
    r"^FROM\s+(alpine:[0-9]+\.[0-9]+@sha256:[0-9a-f]{64})\s+AS\s+(builder|runtime)\s*$", re.MULTILINE
)
FROM_ARG_PATTERN = re.compile(r"^FROM\s+\$\{ALPINE_IMAGE\}\s+AS\s+(builder|runtime)\s*$", re.MULTILINE)
CONFIRMED_ABSENCE = re.compile(
    r"(?:manifest unknown|manifest[^\n]*not found|no such manifest|name unknown|404 not found|"
    r"^ERROR:\s+docker\.io/\S+:\s+not found$)",
    re.IGNORECASE | re.MULTILINE,
)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Resolver = Callable[..., package_locks.PackageLock]
PublicationChecker = Callable[[str, str, str, Runner], bool]


class InputError(RuntimeError):
    """Tracked build inputs cannot be resolved or validated reliably."""


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def read_parent(dockerfile: str) -> str:
    parent_arguments = PARENT_ARG_PATTERN.findall(dockerfile)
    argument_stages = FROM_ARG_PATTERN.findall(dockerfile)
    if len(parent_arguments) == 1 and len(argument_stages) == 2 and set(argument_stages) == set(PROFILES):
        return parent_arguments[0]
    matches = FROM_PATTERN.findall(dockerfile)
    stages = {stage: parent for parent, stage in matches}
    if len(matches) != 2 or set(stages) != set(PROFILES):
        raise InputError("Dockerfile must have one pinned Alpine builder and runtime stage")
    if len(set(stages.values())) != 1:
        raise InputError("Dockerfile builder and runtime stages must share one Alpine parent")
    return stages["runtime"]


def render_parent(dockerfile: str, parent: str) -> str:
    if not PARENT_PATTERN.fullmatch(parent):
        raise InputError("Alpine parent must use an explicit release line and SHA-256 digest")
    current = read_parent(dockerfile)
    if current.partition("@")[0] != parent.partition("@")[0]:
        raise InputError("automatic updates must stay on the selected Alpine release line")
    expected_count = 1 if PARENT_ARG_PATTERN.search(dockerfile) else 2
    rendered, count = re.subn(re.escape(current), parent, dockerfile)
    if count != expected_count:
        raise InputError("Dockerfile Alpine parent replacement was ambiguous")
    return rendered


def resolve_alpine_parent(current_parent: str, runner: Runner = run) -> str:
    if not PARENT_PATTERN.fullmatch(current_parent):
        raise InputError("current Alpine parent must use an explicit release line and SHA-256 digest")
    alpine_tag = current_parent.partition("@")[0]
    command = [
        "docker", "buildx", "imagetools", "inspect", alpine_tag,
        "--format", "{{json .Manifest}}",
    ]
    completed = runner(command)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise InputError(f"failed to resolve {alpine_tag}: {detail}")
    try:
        metadata = json.loads(completed.stdout)
        digest = metadata["digest"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise InputError(f"{alpine_tag} inspection returned invalid metadata") from error
    parent = f"{alpine_tag}@{digest}"
    if not PARENT_PATTERN.fullmatch(parent):
        raise InputError(f"{alpine_tag} inspection returned an invalid manifest digest")
    return parent


def _build_input_paths(root: Path) -> list[Path]:
    paths = [root / "Dockerfile", root / "scripts/apk-lock.sh"]
    paths.extend(path for base in (root / "packages", root / "root")
                 for path in base.rglob("*") if path.is_file() or path.is_symlink())
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def image_inputs_digest(root: Path, overrides: Mapping[Path, str] | None = None) -> str:
    overrides = overrides or {}
    paths = set(_build_input_paths(root))
    paths.update(overrides)
    try:
        return package_locks.input_files_digest(root, tuple(paths), overrides)
    except OSError as error:
        raise InputError(f"failed to hash image build inputs: {error}") from error


def _locks_digest(locks: Mapping[Path, package_locks.PackageLock]) -> str:
    digest = hashlib.sha256()
    for path in sorted(locks, key=lambda item: item.as_posix()):
        digest.update(locks[path].render().encode("utf-8"))
    return digest.hexdigest()


def published_matches(image: str, locks_sha256: str, inputs_sha256: str,
                      runner: Runner = run) -> bool:
    command = [
        "docker", "buildx", "imagetools", "inspect", image,
        "--format", "{{json .}}",
    ]
    completed = runner(command)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        if CONFIRMED_ABSENCE.search(detail):
            return False
        raise InputError(f"failed to inspect published image {image}: {detail}")
    try:
        metadata = json.loads(completed.stdout)
        images = metadata["image"]
        for platform in PLATFORMS:
            labels = images[platform]["config"]["Labels"] or {}
            if labels.get("io.saltydk.apk-locks.sha256") != locks_sha256:
                return False
            if labels.get("io.saltydk.image-inputs.sha256") != inputs_sha256:
                return False
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise InputError(f"published image {image} returned invalid platform metadata") from error
    return True


def _existing_lock(path: Path) -> package_locks.PackageLock | None:
    if not path.exists():
        return None
    return package_locks.parse_lock(path.read_text(encoding="utf-8"))


def _base_report(status: str, images: list[dict[str, object]], changed_files: list[str],
                 *, should_build: bool, inputs_sha256: str | None,
                 locks_sha256: str | None) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": 1,
        "kind": "update",
        "repository": REPOSITORY,
        "status": status,
        "images": images,
        "changed_files": changed_files,
        "outcomes": {},
        "published": [],
        "should_build": should_build,
    }
    if inputs_sha256 is not None:
        report["input_sha"] = inputs_sha256
    if locks_sha256 is not None:
        report["locks_sha256"] = locks_sha256
    return report


def update_repository(root: Path = ROOT, *, write: bool = False, runner: Runner = run,
                      resolver: Resolver = package_locks.resolve_lock,
                      publication_checker: PublicationChecker = published_matches) -> dict[str, object]:
    dockerfile_path = root / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    old_parent = read_parent(dockerfile)
    new_parent = resolve_alpine_parent(old_parent, runner)

    resolved: dict[Path, package_locks.PackageLock] = {}
    images: list[dict[str, object]] = []
    for profile in PROFILES:
        for platform, architecture in package_locks.ARCHITECTURES.items():
            path = package_locks.lock_path(root, profile, architecture)
            old_lock = _existing_lock(path)
            new_lock = resolver(
                root, profile, platform, new_parent,
                helper=root / "scripts/apk-lock.sh",
            )
            resolved[path] = new_lock
            images.append({
                "name": "alpine-s6overlay",
                "platform": platform,
                "stage": profile,
                "changes": package_locks.package_changes(
                    old_lock.packages if old_lock else {}, new_lock.packages
                ),
                "inputs": ([{"name": "base image", "old": old_parent, "new": new_parent}]
                           if old_parent != new_parent else []),
                "lock_digest": new_lock.digest,
            })

    rendered_dockerfile = render_parent(dockerfile, new_parent)
    candidates: dict[Path, str] = {dockerfile_path: rendered_dockerfile}
    candidates.update({path: lock.render() for path, lock in resolved.items()})
    changed = {
        path: content for path, content in candidates.items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    }
    changed_files = sorted(path.relative_to(root).as_posix() for path in changed)
    locks_sha256 = _locks_digest(resolved)
    inputs_sha256 = image_inputs_digest(root, candidates)

    if changed:
        status = "update-available"
        should_build = True
    else:
        published = publication_checker(IMAGE, locks_sha256, inputs_sha256, runner)
        status = "no-changes" if published else "pending-publication"
        should_build = not published

    if write and changed:
        package_locks.write_files(changed)

    return _base_report(
        status, images, changed_files,
        should_build=should_build,
        inputs_sha256=inputs_sha256,
        locks_sha256=locks_sha256,
    )


def verify_repository(root: Path = ROOT) -> dict[str, object]:
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    parent = read_parent(dockerfile)
    images: list[dict[str, object]] = []
    for profile in PROFILES:
        for platform, architecture in package_locks.ARCHITECTURES.items():
            lock = package_locks.validate_lock(root, profile, architecture, parent)
            images.append({
                "name": "alpine-s6overlay",
                "platform": platform,
                "stage": profile,
                "changes": [],
                "inputs": [],
                "lock_digest": lock.digest,
            })
    return _base_report(
        "no-changes", images, [], should_build=False,
        inputs_sha256=image_inputs_digest(root),
        locks_sha256=package_locks.locks_digest(root),
    )


def _write_github_output(path: Path, report: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"changed={str(bool(report['changed_files'])).lower()}\n")
        output.write(f"should-build={str(bool(report['should_build'])).lower()}\n")
        output.write(f"input-sha={report['input_sha']}\n")
        output.write(f"locks-sha256={report['locks_sha256']}\n")
        output.write(f"report={json.dumps(report, separators=(',', ':'))}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    update = subparsers.add_parser("update")
    update.add_argument("--write", action="store_true")
    for command in (update, subparsers.add_parser("verify")):
        command.add_argument("--github-output", type=Path)
        command.add_argument("--report", type=Path)
        command.add_argument("--summary", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = update_repository(write=args.write) if args.command == "update" else verify_repository()
    except (InputError, package_locks.LockError, OSError, RuntimeError) as error:
        report = _base_report(
            "failed", [], [], should_build=False,
            inputs_sha256=None, locks_sha256=None,
        )
        report["error"] = str(error)
        build_report.write_report(report, args.report, args.summary)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    build_report.write_report(report, args.report, args.summary)
    if args.github_output:
        _write_github_output(args.github_output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
