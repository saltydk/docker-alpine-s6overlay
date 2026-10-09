#!/usr/bin/env python3
"""Compare a verified source to the source already published as latest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import build_report, package_locks

PLATFORMS = tuple(package_locks.ARCHITECTURES)


def _ancestor(older: str, newer: str, runner: build_report.Runner) -> bool:
    result = runner(["git", "merge-base", "--is-ancestor", older, newer])
    if result.returncode not in (0, 1):
        raise ValueError(
            f"cannot compare published source history: {result.stderr.strip()}"
        )
    return result.returncode == 0


def decide(
    image: str,
    source_sha: str,
    *,
    default_branch: bool = True,
    runner: build_report.Runner = build_report.run,
) -> dict[str, str]:
    if not build_report.GIT_REVISION.fullmatch(source_sha):
        raise ValueError("candidate source must be a complete Git commit SHA")
    if not default_branch:
        return {"promote": "false", "status": "non-default-branch"}
    reference = image + ":latest"
    result = runner(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .}}",
        ]
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        if build_report._confirmed_absence(detail, reference):
            return {"promote": "true", "status": "first-publication"}
        raise ValueError(f"cannot inspect latest publication: {detail}")
    metadata = json.loads(result.stdout)
    digest = metadata["manifest"]["digest"]
    if not isinstance(digest, str) or not build_report.SHA256_DIGEST.fullmatch(digest):
        raise ValueError("latest publication has no valid manifest digest")
    revisions = {
        metadata["image"][platform]["config"]["Labels"][
            "org.opencontainers.image.revision"
        ]
        for platform in PLATFORMS
    }
    if len(revisions) != 1:
        raise ValueError("latest platforms disagree about their source commit")
    revision = next(iter(revisions))
    if not isinstance(revision, str) or not build_report.GIT_REVISION.fullmatch(
        revision
    ):
        raise ValueError("latest publication has no complete Git source commit")
    decision = {"latest-digest": digest, "latest-source": revision}
    if revision == source_sha:
        return {**decision, "promote": "true", "status": "same-source"}
    shallow = runner(["git", "rev-parse", "--is-shallow-repository"])
    if shallow.returncode or shallow.stdout.strip() != "false":
        raise ValueError("complete Git history is required to compare published source")
    known = runner(["git", "cat-file", "-e", revision + "^{commit}"])
    if known.returncode:
        fetched = runner(["git", "fetch", "--no-tags", "origin", revision])
        if fetched.returncode:
            raise ValueError(
                f"cannot fetch latest source history: {fetched.stderr.strip()}"
            )
    if _ancestor(revision, source_sha, runner):
        return {**decision, "promote": "true", "status": "advance"}
    if _ancestor(source_sha, revision, runner):
        return {**decision, "promote": "false", "status": "superseded"}
    return {**decision, "promote": "false", "status": "divergent"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--default-branch", choices=("true", "false"), required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = decide(
            args.image, args.source_sha, default_branch=args.default_branch == "true"
        )
        with args.github_output.open("a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Latest promotion check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
