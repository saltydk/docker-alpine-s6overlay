import json
import os
import re
import shutil
import subprocess  # nosec B404: subprocesses are trusted fixture commands without a shell.
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "example/base"
CANDIDATE_DIGEST = "sha256:" + "a" * 64
LATEST_DIGEST = "sha256:" + "b" * 64
PLATFORMS = ("linux/amd64", "linux/arm64", "linux/arm/v7")


class GitHistoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        git_binary = shutil.which("git")
        if git_binary is None:
            self.fail("Git is required to create the temporary history")
        self.git_binary = git_binary
        self.git("init", "-q")
        self.git("config", "user.name", "publication tests")
        self.git("config", "user.email", "tests@example.invalid")
        self.first = self.commit("first")
        self.second = self.commit("second")
        self.third = self.commit("third")
        self.git("checkout", "-q", "--detach", self.first)
        self.divergent = self.commit("divergent")
        self.git("checkout", "-q", "--detach", self.third)
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        # This resolved Git executable receives only fixture arguments and generated commit IDs.
        return subprocess.check_output(
            [self.git_binary, *arguments], cwd=self.root, text=True
        ).strip()  # nosec B603

    def commit(self, value: str) -> str:
        (self.root / "source").write_text(value + "\n", encoding="utf-8")
        self.git("add", "source")
        self.git("commit", "-q", "-m", f"test: create {value} source")
        return self.git("rev-parse", "HEAD")

    def metadata(self, revision: str) -> dict:
        return {
            "manifest": {"digest": LATEST_DIGEST},
            "image": {
                platform: {
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.revision": revision,
                        }
                    }
                }
                for platform in PLATFORMS
            },
        }

    def runner(
        self, revision: str, *, error: str | None = None, metadata: object | None = None
    ):
        def run(command: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(command)
            if command[0] == "docker":
                if error:
                    return subprocess.CompletedProcess(command, 1, "", error)
                payload = self.metadata(revision) if metadata is None else metadata
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            if command[:2] == ["git", "fetch"]:
                return subprocess.CompletedProcess(command, 1, "", "source unavailable")
            # The guard emits fixed local Git inspections; Docker and fetch are intercepted above.
            return subprocess.run(
                command, cwd=self.root, capture_output=True, text=True, check=False
            )  # nosec B603

        return run


class LatestPromotionTests(GitHistoryFixture):
    def decide(self, source: str, published: str, **kwargs) -> dict[str, str]:
        from scripts.check_latest_promotion import decide

        return decide(IMAGE, source, runner=self.runner(published, **kwargs))

    def test_branch_head_movement_does_not_prevent_advancing_published_source(
        self,
    ) -> None:
        decision = self.decide(self.second, self.first)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.third)
        self.assertEqual(decision["promote"], "true")
        self.assertEqual(decision["status"], "advance")
        self.assertFalse(
            any("refs/heads" in " ".join(command) for command in self.calls)
        )

    def test_older_source_is_a_successful_no_op(self) -> None:
        decision = self.decide(self.first, self.second)
        self.assertEqual(decision["promote"], "false")
        self.assertEqual(decision["status"], "superseded")

    def test_same_source_can_retry(self) -> None:
        decision = self.decide(self.second, self.second)
        self.assertEqual(decision["promote"], "true")
        self.assertEqual(decision["status"], "same-source")

    def test_divergent_source_is_a_successful_no_op(self) -> None:
        decision = self.decide(self.divergent, self.second)
        self.assertEqual(decision["promote"], "false")
        self.assertEqual(decision["status"], "divergent")

    def test_nondefault_branch_preserves_latest_without_registry_access(self) -> None:
        from scripts.check_latest_promotion import decide

        decision = decide(
            IMAGE, self.second, default_branch=False, runner=self.runner(self.first)
        )
        self.assertEqual(decision["promote"], "false")
        self.assertEqual(decision["status"], "non-default-branch")
        self.assertEqual(self.calls, [])

    def test_first_publication_requires_confirmed_absence(self) -> None:
        errors = (
            "manifest unknown",
            "MANIFEST_UNKNOWN",
            "name unknown",
            f"ERROR: {IMAGE}:latest: not found",
            f"ERROR: docker.io/{IMAGE}:latest: not found",
        )
        for error in errors:
            with self.subTest(error=error):
                decision = self.decide(self.first, self.first, error=error)
                self.assertEqual(decision["promote"], "true")
                self.assertEqual(decision["status"], "first-publication")

    def test_registry_and_authentication_errors_cannot_authorize_promotion(
        self,
    ) -> None:
        errors = (
            "registry timed out",
            "unauthorized: manifest unknown",
            "denied",
            "ERROR: failed to authorize: credential helper: not found",
            "ERROR: resolving registry address: not found",
            "ERROR: failed to fetch manifest: credential helper: not found",
            "ERROR: unrelated/image:latest: not found",
            "ERROR: fetching manifest: not found",
        )
        for error in errors:
            with self.subTest(error=error), self.assertRaises(ValueError):
                self.decide(self.first, self.first, error=error)

    def test_malformed_metadata_cannot_authorize_promotion(self) -> None:
        malformed = [
            [],
            {},
            {"manifest": {"digest": "invalid"}},
            self.metadata("invalid"),
        ]
        for metadata in malformed:
            with (
                self.subTest(metadata=metadata),
                self.assertRaises((ValueError, KeyError, TypeError)),
            ):
                self.decide(self.second, self.first, metadata=metadata)

    def test_mixed_platform_revisions_cannot_authorize_promotion(self) -> None:
        metadata = self.metadata(self.first)
        metadata["image"]["linux/arm64"]["config"]["Labels"][
            "org.opencontainers.image.revision"
        ] = self.second
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.decide(self.second, self.first, metadata=metadata)

    def test_unavailable_source_history_cannot_authorize_promotion(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot fetch"):
            self.decide(self.second, "f" * 40)

    def test_shallow_source_history_cannot_authorize_promotion(self) -> None:
        (self.root / ".git/shallow").write_text(self.second + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "complete Git history"):
            self.decide(self.third, self.first)

    def test_git_comparison_error_cannot_authorize_promotion(self) -> None:
        from scripts.check_latest_promotion import decide

        delegate = self.runner(self.first)

        def run(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["git", "merge-base"]:
                return subprocess.CompletedProcess(
                    command, 128, "", "history unavailable"
                )
            return delegate(command)

        with self.assertRaisesRegex(ValueError, "cannot compare"):
            decide(IMAGE, self.second, runner=run)


class PublicationCliTests(GitHistoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.workflow = (ROOT / ".github/workflows/build.yml").read_text(
            encoding="utf-8"
        )
        self.state = self.root / "registry.json"
        self.output = self.root / "github-output"
        self.binary = self.root / "bin"
        self.binary.mkdir()
        docker = self.binary / "docker"
        docker.write_text(
            textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys
            state_path = Path(os.environ['TEST_REGISTRY'])
            state = json.loads(state_path.read_text())
            state.setdefault('commands', []).append(sys.argv[1:])
            state_path.write_text(json.dumps(state))
            if sys.argv[1:4] == ['buildx', 'imagetools', 'inspect']:
                error = state.get('error') or (
                    state.get('verification_error') if state.get('promoted') else None)
                if error:
                    print(error, file=sys.stderr)
                    raise SystemExit(1)
                manifest = {'digest': state['latest']}
                if sys.argv[-1] == '--raw':
                    print(json.dumps(state.get('attestation', {'layers': [{'annotations': {
                        'in-toto.io/predicate-type': 'https://spdx.dev/Document'}}]})))
                elif sys.argv[-1] == '{{json .Manifest}}':
                    print(json.dumps(manifest))
                elif 'metadata' in state:
                    print(json.dumps(state['metadata']))
                else:
                    platforms = ('linux/amd64', 'linux/arm64', 'linux/arm/v7')
                    print(json.dumps({'manifest': manifest, 'image': {
                        platform: {'config': {'Labels': {
                            'org.opencontainers.image.revision': state['revision']}}}
                        for platform in platforms}}))
            elif sys.argv[1:5] == ['buildx', 'imagetools', 'create', '--tag']:
                state['latest'] = sys.argv[-1].split('@')[1]
                state['promoted'] = True
                state_path.write_text(json.dumps(state))
            elif sys.argv[1] == 'run':
                print('__APK_LOCK_ARCH__=x86_64')
                print('__APK_LOCK_DIGEST__=')
                print('__APK_LOCK_CONTENT__')
                print('__APK_LOCK_INVENTORY__')
                print('busybox=1.0-r0')
            else:
                raise SystemExit('unexpected Docker command')
            """),
            encoding="utf-8",
        )
        docker.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.binary) + ":" + os.environ["PATH"],
            "TEST_REGISTRY": str(self.state),
        }

    def cli(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        # The interpreter and repository script paths are trusted; arguments never pass through a shell.
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *arguments],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )  # nosec B603

    def publish(
        self,
        source: str,
        published: str,
        *,
        default_branch: bool = True,
        error: str | None = None,
        verification_error: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.state.write_text(
            json.dumps(
                {
                    "latest": LATEST_DIGEST,
                    "revision": published,
                    "error": error,
                    "verification_error": verification_error,
                }
            ),
            encoding="utf-8",
        )
        self.git("checkout", "-q", "--detach", source)
        result = self.cli(
            "publication.py",
            "record",
            "--reference",
            f"{IMAGE}:sha-{source}",
            "--digest",
            CANDIDATE_DIGEST,
            "--output",
            "published-sha.txt",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli(
            "check_latest_promotion.py",
            "--image",
            IMAGE,
            "--source-sha",
            source,
            "--default-branch",
            str(default_branch).lower(),
            "--github-output",
            str(self.output),
        )
        if result.returncode:
            return result
        outputs = dict(
            line.split("=", 1) for line in self.output.read_text().splitlines()
        )
        if outputs["promote"] != "true":
            return result
        for command, output in (
            ("promote", "promoted-latest.txt"),
            ("verify", None),
            ("record", "published-latest.txt"),
        ):
            arguments = [
                command,
                "--reference",
                f"{IMAGE}:latest",
                "--digest",
                CANDIDATE_DIGEST,
            ]
            if output:
                arguments.extend(("--output", output))
            result = self.cli("publication.py", *arguments)
            if result.returncode:
                return result
        return result

    def test_older_run_preserves_latest_and_records_only_immutable_publication(
        self,
    ) -> None:
        result = self.publish(self.first, self.second)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["latest"], LATEST_DIGEST)
        self.assertEqual(
            (self.root / "published-sha.txt").read_text(),
            f"{IMAGE}:sha-{self.first}@{CANDIDATE_DIGEST}\n",
        )
        self.assertFalse((self.root / "published-latest.txt").exists())
        self.assertFalse((self.root / "promoted-latest.txt").exists())

    def test_current_run_promotes_and_records_verified_latest(self) -> None:
        result = self.publish(self.second, self.first)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["latest"], CANDIDATE_DIGEST)
        self.assertEqual(
            (self.root / "published-latest.txt").read_text(),
            f"{IMAGE}:latest@{CANDIDATE_DIGEST}\n",
        )

    def test_nondefault_branch_publishes_sha_without_changing_latest(self) -> None:
        result = self.publish(self.second, self.first, default_branch=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["latest"], LATEST_DIGEST)
        self.assertTrue((self.root / "published-sha.txt").exists())
        self.assertFalse((self.root / "published-latest.txt").exists())

    def test_inspection_failure_preserves_latest_and_is_visible(self) -> None:
        result = self.publish(self.second, self.first, error="unauthorized")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthorized", result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["latest"], LATEST_DIGEST)
        self.assertFalse((self.root / "published-latest.txt").exists())

    def test_successful_promotion_retains_evidence_when_verification_fails(
        self,
    ) -> None:
        result = self.publish(
            self.second, self.first, verification_error="registry timed out"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registry timed out", result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["latest"], CANDIDATE_DIGEST)
        self.assertEqual(
            (self.root / "promoted-latest.txt").read_text(),
            f"{IMAGE}:latest@{CANDIDATE_DIGEST}\n",
        )
        self.assertFalse((self.root / "published-latest.txt").exists())
        self.assertIn(
            "Capture publication outcomes\n        if: always()", self.workflow
        )
        self.assertIn("Upload publication records\n        if: always()", self.workflow)
        self.assertIn("promoted-*.txt", self.workflow)

    def test_outcomes_cli_retains_failure_and_partial_write_status(self) -> None:
        result = self.cli(
            "publication.py",
            "outcomes",
            "--output",
            "publication-outcomes.txt",
            "--outcome",
            "promote=success",
            "--outcome",
            "verify-latest=failure",
            "--outcome",
            "record-latest=skipped",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.root / "publication-outcomes.txt").read_text(),
            "promote=success\nverify-latest=failure\nrecord-latest=skipped\n",
        )

    def test_invalid_digest_cannot_write_alias_or_publication_record(self) -> None:
        self.state.write_text(
            json.dumps({"latest": LATEST_DIGEST, "revision": self.first})
        )
        result = self.cli(
            "publication.py",
            "promote",
            "--reference",
            f"{IMAGE}:latest",
            "--digest",
            "invalid",
            "--output",
            "promoted-latest.txt",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())["latest"], LATEST_DIGEST)
        self.assertFalse((self.root / "promoted-latest.txt").exists())

    def test_verification_cli_rejects_a_different_published_digest(self) -> None:
        self.state.write_text(
            json.dumps({"latest": LATEST_DIGEST, "revision": self.first})
        )
        result = self.cli(
            "publication.py",
            "verify",
            "--reference",
            f"{IMAGE}:latest",
            "--digest",
            CANDIDATE_DIGEST,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("published image digest differs", result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["latest"], LATEST_DIGEST)

    def inspect_sha(self) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "publication.py",
            "inspect-sha",
            "--repository",
            "example/base",
            "--image-name",
            "base",
            "--platform",
            "linux/amd64",
            "--stage",
            "runtime",
            "--reference",
            f"{IMAGE}:sha-{self.second}",
            "--source-sha",
            self.second,
            "--output",
            "exact-sha-tag.json",
            "--github-output",
            str(self.output),
        )

    def test_immutable_inspection_cli_selects_existing_digest(self) -> None:
        self.state.write_text(
            json.dumps({"latest": CANDIDATE_DIGEST, "revision": self.second})
        )
        result = self.inspect_sha()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.output.read_text(), f"exists=true\ndigest={CANDIDATE_DIGEST}\n"
        )

    def test_immutable_inspection_cli_emits_absence_only_when_confirmed(self) -> None:
        self.state.write_text(json.dumps({"error": "manifest unknown"}))
        result = self.inspect_sha()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "exists=false\n")

    def test_immutable_inspection_cli_preserves_error_without_false_absence(
        self,
    ) -> None:
        self.state.write_text(
            json.dumps(
                {"error": "failed to fetch manifest: credential helper: not found"}
            )
        )
        result = self.inspect_sha()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        report = json.loads((self.root / "exact-sha-tag.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["images"][0]["baseline"]["status"], "error")

    def test_effective_digest_cli_prefers_existing_sha_and_falls_back_to_build(
        self,
    ) -> None:
        for existing, expected in (
            (LATEST_DIGEST, LATEST_DIGEST),
            ("", CANDIDATE_DIGEST),
        ):
            with self.subTest(existing=existing):
                result = self.cli(
                    "publication.py",
                    "digest",
                    "--built-digest",
                    CANDIDATE_DIGEST,
                    "--existing-digest",
                    existing,
                    "--github-output",
                    str(self.output),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.output.read_text().splitlines()[-1], f"digest={expected}"
                )

    def sha_metadata(self) -> dict:
        descriptors, images = [], {}
        for number, platform in enumerate(PLATFORMS, 1):
            os_name, architecture, *variant = platform.split("/")
            platform_fields = {"os": os_name, "architecture": architecture}
            if variant:
                platform_fields["variant"] = variant[0]
            digest = "sha256:" + str(number) * 64
            descriptors.append({"digest": digest, "platform": platform_fields})
            descriptors.append(
                {
                    "digest": "sha256:" + str(number + 3) * 64,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                    "annotations": {
                        "vnd.docker.reference.type": "attestation-manifest",
                        "vnd.docker.reference.digest": digest,
                    },
                }
            )
            images[platform] = {
                "config": {
                    "Labels": {
                        "org.opencontainers.image.revision": self.second,
                        "io.saltydk.apk-locks.sha256": "c" * 64,
                        "io.saltydk.image-inputs.sha256": "d" * 64,
                    }
                }
            }
        return {
            "manifest": {"digest": CANDIDATE_DIGEST, "manifests": descriptors},
            "image": images,
        }

    def verify_sha(
        self, metadata: dict, *, attestation: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        state = {
            "latest": CANDIDATE_DIGEST,
            "revision": self.second,
            "metadata": metadata,
        }
        if attestation is not None:
            state["attestation"] = attestation
        self.state.write_text(json.dumps(state))
        return self.cli(
            "publication.py",
            "verify-sha",
            "--reference",
            f"{IMAGE}:sha-{self.second}",
            "--digest",
            CANDIDATE_DIGEST,
            "--source-sha",
            self.second,
            "--locks-sha",
            "c" * 64,
            "--input-sha",
            "d" * 64,
        )

    def test_sha_verification_cli_checks_all_platforms_and_digest_pinned_sboms(
        self,
    ) -> None:
        result = self.verify_sha(self.sha_metadata())
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = json.loads(self.state.read_text())["commands"]
        raw = [command for command in commands if command[-1] == "--raw"]
        self.assertEqual(len(raw), 3)
        self.assertTrue(
            all(command[3].startswith(IMAGE + "@sha256:") for command in raw)
        )

    def test_sha_verification_cli_rejects_manifest_platform_label_or_sbom_mismatch(
        self,
    ) -> None:
        for field in ("digest", "platform", "label", "subject", "predicate"):
            with self.subTest(field=field):
                metadata = self.sha_metadata()
                attestation = None
                if field == "digest":
                    metadata["manifest"]["digest"] = LATEST_DIGEST
                elif field == "platform":
                    metadata["manifest"]["manifests"][0]["platform"]["variant"] = ""
                elif field == "label":
                    metadata["image"]["linux/arm64"]["config"]["Labels"][
                        "org.opencontainers.image.revision"
                    ] = self.first
                elif field == "subject":
                    metadata["manifest"]["manifests"][1]["annotations"][
                        "vnd.docker.reference.digest"
                    ] = LATEST_DIGEST
                else:
                    attestation = {
                        "layers": [
                            {"annotations": {"in-toto.io/predicate-type": "unexpected"}}
                        ]
                    }
                result = self.verify_sha(metadata, attestation=attestation)
                self.assertNotEqual(result.returncode, 0)

    def test_workflow_reports_only_verified_promotion_to_follow_up_scan(self) -> None:
        publication = self.workflow.split("\n  publish:\n", 1)[1].split(
            "\n  report:\n", 1
        )[0]
        self.assertIn("fetch-depth: 0", publication)
        promoted_match = re.search(r"(?m)^      latest-promoted: (.+)$", publication)
        if promoted_match is None:
            self.fail("publication must expose its verified-promotion output")
        promoted = promoted_match.group(1)
        for step in ("promote", "verify-latest", "record-latest"):
            self.assertIn(f"steps.{step}.outcome == 'success'", promoted)
        scan = self.workflow.split("\n  scan-published:\n", 1)[1].split(
            "\n  publish:\n", 1
        )[0]
        self.assertIn("needs.publish.outputs.latest-promoted == 'true'", scan)
        for operation in ("promote", "verify", "record", "outcomes"):
            self.assertIn(f"python3 scripts/publication.py {operation}", publication)


if __name__ == "__main__":
    unittest.main()
