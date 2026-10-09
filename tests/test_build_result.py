import json
import subprocess  # nosec B404: tests invoke trusted Python scripts without a shell.
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.package_locks import locks_digest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64


class BuildResultCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "build-report.json"
        self.defaults = {
            "static-result": "success",
            "candidate-result": "success",
            "published-scan-result": "skipped",
            "publish-result": "success",
            "should-build": "true",
            "should-publish": "true",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        values = {**self.defaults, **overrides}
        if command == "report":
            values.update(
                {
                    "repository": "example/base",
                    "source-sha": SOURCE,
                    "source-root": str(self.root),
                    "candidate-reports": "candidate-reports",
                    "update-reports": "update-reports",
                    "publication-records": "publication-records",
                    "output": str(self.output),
                    "summary": str(self.root / "summary.md"),
                }
            )
        else:
            values.setdefault("scan-published", "false")
        arguments = [sys.executable, str(ROOT / "scripts/build_result.py"), command]
        for name, value in values.items():
            arguments.extend(("--" + name, value))
        # The interpreter/script are trusted paths; all fixture values are separate argv entries.
        return subprocess.run(
            arguments, cwd=self.root, capture_output=True, text=True, check=False
        )  # nosec B603

    def test_report_cli_preserves_build_publish_and_no_build_statuses(self) -> None:
        cases: tuple[tuple[dict[str, str], str], ...] = (
            ({}, "published"),
            ({"should-publish": "false", "publish-result": "skipped"}, "built"),
            ({"publish-result": "failure"}, "failed"),
            ({"candidate-result": "failure"}, "failed"),
            (
                {
                    "should-build": "false",
                    "candidate-result": "skipped",
                    "publish-result": "skipped",
                    "published-scan-result": "success",
                },
                "no-build-required",
            ),
            ({"should-build": "false", "published-scan-result": "failure"}, "failed"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                result = self.cli("report", **overrides)
                self.assertEqual(
                    result.returncode, 1 if expected == "failed" else 0, result.stderr
                )
                self.assertEqual(
                    json.loads(self.output.read_text())["status"], expected
                )

    def test_required_jobs_cli_preserves_success_and_failure_gates(self) -> None:
        cases: tuple[tuple[dict[str, str], bool], ...] = (
            ({}, True),
            ({"should-publish": "false", "publish-result": "skipped"}, True),
            (
                {
                    "should-build": "false",
                    "candidate-result": "skipped",
                    "publish-result": "skipped",
                    "scan-published": "true",
                    "published-scan-result": "success",
                },
                True,
            ),
            ({"static-result": "failure"}, False),
            ({"candidate-result": "failure"}, False),
            ({"publish-result": "failure"}, False),
            ({"should-build": "false", "scan-published": "false"}, False),
            (
                {
                    "should-build": "false",
                    "scan-published": "true",
                    "published-scan-result": "failure",
                },
                False,
            ),
        )
        for overrides, accepted in cases:
            with self.subTest(overrides=overrides):
                result = self.cli("require", **overrides)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_report_cli_merges_nested_evidence_and_only_verified_publication_receipts(
        self,
    ) -> None:
        candidates = self.root / "candidate-reports/amd64"
        candidates.mkdir(parents=True)
        (candidates / "runtime.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "build",
                    "status": "built",
                    "images": [],
                    "changed_files": ["Dockerfile"],
                    "outcomes": {"candidate-runtime": "success"},
                }
            )
        )
        records = self.root / "publication-records"
        records.mkdir()
        immutable = f"example/base:sha-{SOURCE}@{DIGEST}"
        (records / "published-sha.txt").write_text(immutable + "\n")
        (records / "published-duplicate.txt").write_text(immutable + "\n")
        (records / "promoted-latest.txt").write_text(f"example/base:latest@{DIGEST}\n")
        (records / "publication-outcomes.txt").write_text(
            "promote=success\nverify-latest=failure\nrecord-latest=skipped\nlatest-status=\n"
        )
        result = self.cli("report", **{"publish-result": "failure"})
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["published"], [immutable])
        self.assertEqual(report["changed_files"], ["Dockerfile"])
        self.assertEqual(report["outcomes"]["publication-promote"], "success")
        self.assertEqual(report["outcomes"]["publication-verify-latest"], "failure")
        self.assertEqual(report["outcomes"]["publication-latest-status"], "unknown")
        self.assertIn("Build report", (self.root / "summary.md").read_text())

    def test_preparation_cli_exposes_existing_lock_digest_for_static_job_output(
        self,
    ) -> None:
        output = self.root / "github-output"
        # Run the repository's fixed verification script through this interpreter, without a shell.
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/manage_inputs.py"),
                "verify",
                "--github-output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )  # nosec B603
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values["locks-sha256"], locks_digest(ROOT))
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        self.assertIn("locks-sha: ${{ steps.inputs.outputs.locks-sha256 }}", workflow)


if __name__ == "__main__":
    unittest.main()
