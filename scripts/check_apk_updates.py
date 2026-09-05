#!/usr/bin/env python3
"""Check published Alpine image platforms for installable package upgrades."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Sequence


DEFAULT_PLATFORMS = ("linux/amd64", "linux/arm64", "linux/arm/v7")
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class UpdateCheckError(RuntimeError):
    """Raised when an image platform cannot be checked reliably."""


def parse_apk_updates(output: str) -> tuple[str, ...]:
    updates: list[str] = []
    for line in output.splitlines():
        if re.search(r"\s<\s", line):
            updates.append(" ".join(line.split()))
    return tuple(updates)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def check_platforms(image: str, platforms: Sequence[str], runner: Runner = _run) -> dict[str, object]:
    results: list[dict[str, object]] = []
    needs_refresh = False

    for platform in platforms:
        command = [
            "docker",
            "run",
            "--pull=always",
            "--rm",
            "--platform",
            platform,
            "--entrypoint",
            "/bin/sh",
            image,
            "-ec",
            'apk update >/dev/null; apk version -l "<"',
        ]
        completed = runner(command)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise UpdateCheckError(f"failed to check {platform}: {detail}")

        updates = list(parse_apk_updates(completed.stdout))
        needs_refresh = needs_refresh or bool(updates)
        results.append({"platform": platform, "updates": updates})

    return {"image": image, "needs_refresh": needs_refresh, "platforms": results}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="saltydk/alpine-s6overlay:latest")
    parser.add_argument("--platform", action="append", dest="platforms")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    platforms = tuple(args.platforms or DEFAULT_PLATFORMS)
    try:
        report = check_platforms(args.image, platforms)
    except UpdateCheckError as error:
        print(str(error), file=sys.stderr)
        return 1

    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"needs-refresh={str(report['needs_refresh']).lower()}\n")
            output.write(f"report={json.dumps(report, separators=(',', ':'))}\n")

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
