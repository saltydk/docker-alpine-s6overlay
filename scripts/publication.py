#!/usr/bin/env python3
"""Promote, verify, and record container publication through explicit arguments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import build_report


def record(reference: str, digest: str, output: Path) -> None:
    output.write_text(f"{reference}@{digest}\n", encoding="utf-8")


def promote(
    reference: str,
    digest: str,
    output: Path,
    runner: build_report.Runner = build_report.run,
) -> None:
    repository, separator, tag = reference.rpartition(":")
    if not separator or not tag or "/" in tag or "@" in reference:
        raise ValueError("promotion requires an explicit image tag")
    result = runner(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            reference,
            f"{repository}@{digest}",
        ]
    )
    if result.returncode:
        raise ValueError(
            f"image promotion failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    # Retain the completed write even if later verification fails.
    record(reference, digest, output)


def verify(
    reference: str, digest: str, runner: build_report.Runner = build_report.run
) -> None:
    result = runner(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest}}",
        ]
    )
    if result.returncode:
        raise ValueError(
            f"published image inspection failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    published = json.loads(result.stdout)["digest"]
    if published != digest:
        raise ValueError(
            f"published image digest differs: expected {digest}, got {published}"
        )


def verify_sha(
    reference: str,
    digest: str,
    source_sha: str,
    locks_sha: str,
    input_sha: str,
    runner: build_report.Runner = build_report.run,
) -> None:
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
        raise ValueError(
            f"published SHA inspection failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    details = json.loads(result.stdout)
    manifest = details["manifest"]
    if manifest["digest"] != digest:
        raise ValueError("published SHA manifest digest differs")
    descriptors = manifest["manifests"]
    images = [
        row for row in descriptors if row["platform"]["architecture"] != "unknown"
    ]
    platforms = []
    for row in images:
        platform = row["platform"]
        name = platform["os"] + "/" + platform["architecture"]
        variant = platform.get("variant")
        if variant is not None and variant is not False:
            name += "/" + variant
        platforms.append(name)
    expected = sorted(build_report.PLATFORM_ARCHITECTURES)
    if sorted(platforms) != expected:
        raise ValueError("published SHA platforms differ")
    attestations = [
        row
        for row in descriptors
        if (row.get("annotations") or {}).get("vnd.docker.reference.type")
        == "attestation-manifest"
    ]
    subjects = sorted(
        row["annotations"]["vnd.docker.reference.digest"] for row in attestations
    )
    if subjects != sorted(row["digest"] for row in images):
        raise ValueError("published SBOM subjects differ from image manifests")
    wanted = {
        "org.opencontainers.image.revision": source_sha,
        "io.saltydk.apk-locks.sha256": locks_sha,
        "io.saltydk.image-inputs.sha256": input_sha,
    }
    for platform in expected:
        labels = details["image"][platform]["config"]["Labels"]
        if any(labels.get(key) != value for key, value in wanted.items()):
            raise ValueError(f"published SHA source/input labels differ for {platform}")
    repository = reference.rsplit(":", 1)[0]
    for descriptor in attestations:
        result = runner(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                f"{repository}@{descriptor['digest']}",
                "--raw",
            ]
        )
        if result.returncode:
            raise ValueError(
                f"published SBOM inspection failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        layers = json.loads(result.stdout)["layers"]
        predicates = [
            (layer.get("annotations") or {}).get("in-toto.io/predicate-type")
            for layer in layers
        ]
        if predicates != ["https://spdx.dev/Document"]:
            raise ValueError("published attestation is not exactly one SPDX document")


def inspect_sha(args: argparse.Namespace) -> int:
    status = build_report.main(
        [
            "snapshot",
            "--repository",
            args.repository,
            "--image",
            args.image_name,
            "--platform",
            args.platform,
            "--stage",
            args.stage,
            "--reference",
            args.reference,
            "--source-sha",
            args.source_sha,
            "--output",
            str(args.output),
        ]
    )
    if status:
        return status
    baseline = json.loads(args.output.read_text(encoding="utf-8"))["images"][0][
        "baseline"
    ]
    if baseline["status"] == "absent":
        output = "exists=false\n"
    elif baseline["status"] == "available":
        digest = baseline["digest"]
        if not build_report.SHA256_DIGEST.fullmatch(digest):
            raise ValueError("immutable SHA tag has an invalid manifest digest")
        output = f"exists=true\ndigest={digest}\n"
    else:
        raise ValueError(
            f"SHA tag inspection returned unexpected baseline status: {baseline['status']}"
        )
    with args.github_output.open("a", encoding="utf-8") as stream:
        stream.write(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("promote", "verify", "verify-sha", "record"):
        command = commands.add_parser(name)
        command.add_argument("--reference", required=True)
        command.add_argument("--digest", required=True)
        if name in ("promote", "record"):
            command.add_argument("--output", type=Path, required=True)
        if name == "verify-sha":
            command.add_argument("--source-sha", required=True)
            command.add_argument("--locks-sha", required=True)
            command.add_argument("--input-sha", required=True)
    snapshot = commands.add_parser("inspect-sha")
    for name in ("repository", "image-name", "reference", "source-sha"):
        snapshot.add_argument("--" + name, required=True)
    snapshot.add_argument(
        "--platform", choices=tuple(build_report.PLATFORM_ARCHITECTURES), required=True
    )
    snapshot.add_argument("--stage", choices=build_report.STAGES, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--github-output", type=Path, required=True)
    effective = commands.add_parser("digest")
    effective.add_argument("--built-digest", default="")
    effective.add_argument("--existing-digest", default="")
    effective.add_argument("--github-output", type=Path, required=True)
    outcomes = commands.add_parser("outcomes")
    outcomes.add_argument("--output", type=Path, required=True)
    outcomes.add_argument("--outcome", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "inspect-sha":
            return inspect_sha(args)
        if args.command == "digest":
            digest = args.existing_digest or args.built_digest
            if not build_report.SHA256_DIGEST.fullmatch(digest):
                raise ValueError("publication requires a SHA-256 manifest digest")
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"digest={digest}\n")
        elif args.command == "outcomes":
            if any(
                "=" not in value or "\n" in value or "\r" in value
                for value in args.outcome
            ):
                raise ValueError(
                    "publication outcomes must be single-line key=value arguments"
                )
            args.output.write_text(
                "".join(value + "\n" for value in args.outcome), encoding="utf-8"
            )
        else:
            if not build_report.SHA256_DIGEST.fullmatch(args.digest):
                raise ValueError("publication requires a SHA-256 manifest digest")
            if args.command == "promote":
                promote(args.reference, args.digest, args.output)
            elif args.command == "verify":
                verify(args.reference, args.digest)
            elif args.command == "verify-sha":
                verify_sha(
                    args.reference,
                    args.digest,
                    args.source_sha,
                    args.locks_sha,
                    args.input_sha,
                )
            else:
                record(args.reference, args.digest, args.output)
        return 0
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Publication {args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
