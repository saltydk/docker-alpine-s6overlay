#!/usr/bin/env python3
"""Aggregate build receipts and check required jobs from explicit arguments."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import build_report


def report_status(args: argparse.Namespace) -> str:
    if args.should_build == "true" and args.candidate_result == "success":
        if args.should_publish == "true":
            return "published" if args.publish_result == "success" else "failed"
        return "built"
    if (
        args.should_build == "false"
        and args.static_result == "success"
        and args.published_scan_result == "success"
    ):
        return "no-build-required"
    return "failed"


def files(directory: Path, pattern: str) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob(pattern)
        if path.is_file() and not path.is_symlink()
    )


def aggregate(args: argparse.Namespace) -> int:
    for directory in (
        args.candidate_reports,
        args.update_reports,
        args.publication_records,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    arguments = [
        "aggregate",
        "--repository",
        args.repository,
        "--kind",
        "build",
        "--status",
        report_status(args),
        "--source-sha",
        args.source_sha,
        "--source-root",
        str(args.source_root),
        "--output",
        str(args.output),
        "--summary",
        str(args.summary),
    ]
    outcomes = {
        "static": args.static_result,
        "candidates": args.candidate_result,
        "published-scan": args.published_scan_result,
        "publish": args.publish_result,
    }
    for key, value in outcomes.items():
        arguments.extend(("--outcome", f"{key}={value}"))
    for path in sorted(
        files(args.candidate_reports, "*.json") + files(args.update_reports, "*.json")
    ):
        arguments.extend(("--image-report", str(path)))
    published = {
        line
        for path in files(args.publication_records, "published-*.txt")
        for line in path.read_text(encoding="utf-8").splitlines()
    }
    for reference in sorted(published):
        arguments.extend(("--published", reference))
    for path in files(args.publication_records, "publication-outcomes.txt"):
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key:
                arguments.extend(
                    ("--outcome", f"publication-{key}={value or 'unknown'}")
                )
    return build_report.main(arguments)


def require(args: argparse.Namespace) -> None:
    if args.static_result != "success":
        raise ValueError(f"required job static did not succeed: {args.static_result}")
    required = {}
    if args.should_build == "true":
        required["candidates"] = args.candidate_result
        if args.should_publish == "true":
            required["publish"] = args.publish_result
    else:
        if args.scan_published != "true":
            raise ValueError("a no-build run requires the published-image scan")
        required["published-scan"] = args.published_scan_result
    for name, status in required.items():
        if status != "success":
            raise ValueError(f"required job {name} did not succeed: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("report", "require"):
        command = commands.add_parser(name)
        for field in (
            "static-result",
            "candidate-result",
            "published-scan-result",
            "publish-result",
        ):
            command.add_argument("--" + field, required=True)
        for field in ("should-build", "should-publish"):
            command.add_argument("--" + field, choices=("true", "false"), required=True)
        if name == "require":
            command.add_argument(
                "--scan-published", choices=("true", "false"), required=True
            )
        else:
            for field in ("repository", "source-sha"):
                command.add_argument("--" + field, required=True)
            for field in (
                "source-root",
                "candidate-reports",
                "update-reports",
                "publication-records",
                "output",
                "summary",
            ):
                command.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "report":
            return aggregate(args)
        require(args)
        return 0
    except (OSError, ValueError, build_report.ReportError) as error:
        print(f"Build result {args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
