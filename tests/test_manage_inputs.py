import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.manage_inputs import (
    InputError,
    image_inputs_digest,
    published_matches,
    read_parent,
    resolve_alpine_parent,
    update_repository,
)
from scripts.package_locks import PackageLock, requests_digest


OLD_PARENT = "alpine:3.24@sha256:" + "a" * 64
NEW_PARENT = "alpine:3.24@sha256:" + "b" * 64
PLATFORMS = {
    "linux/amd64": "x86_64",
    "linux/arm64": "aarch64",
    "linux/arm/v7": "armv7",
}


class ManageInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dockerfile = self.root / "Dockerfile"
        self.dockerfile.write_text(
            f"ARG ALPINE_IMAGE={OLD_PARENT}\n"
            "FROM ${ALPINE_IMAGE} AS builder\n"
            "FROM ${ALPINE_IMAGE} AS runtime\n",
            encoding="utf-8",
        )
        (self.root / "scripts").mkdir()
        (self.root / "scripts/apk-lock.sh").write_text("helper\n", encoding="utf-8")
        (self.root / "root/etc").mkdir(parents=True)
        (self.root / "root/etc/example").write_text("runtime\n", encoding="utf-8")
        for profile in ("builder", "runtime"):
            directory = self.root / "packages" / profile
            directory.mkdir(parents=True)
            (directory / "requested.txt").write_text(f"{profile}-package\n", encoding="utf-8")
            for architecture in PLATFORMS.values():
                lock = PackageLock(
                    architecture,
                    OLD_PARENT,
                    requests_digest((f"{profile}-package",)),
                    {"musl": "1-r0"},
                )
                (directory / f"{architecture}.lock").write_text(lock.render(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def parent_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps({"digest": "sha256:" + "b" * 64}), "")

    @staticmethod
    def resolver(root: Path, profile: str, platform: str, parent: str, **kwargs) -> PackageLock:
        architecture = PLATFORMS[platform]
        return PackageLock(
            architecture,
            parent,
            requests_digest((f"{profile}-package",)),
            {"musl": "2-r0", f"{profile}-package": "1-r0"},
        )

    def test_parent_parser_requires_both_stages_to_share_one_pinned_alpine(self) -> None:
        self.assertEqual(read_parent(self.dockerfile.read_text(encoding="utf-8")), OLD_PARENT)
        with self.assertRaises(InputError):
            read_parent(self.dockerfile.read_text().replace("AS runtime", "AS other"))

    def test_alpine_resolution_retains_release_line_and_requires_a_digest(self) -> None:
        self.assertEqual(resolve_alpine_parent(OLD_PARENT, self.parent_runner), NEW_PARENT)

        def invalid(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"digest": "latest"}), "")

        with self.assertRaises(InputError):
            resolve_alpine_parent(OLD_PARENT, invalid)

    def test_alpine_resolution_uses_an_explicitly_selected_future_release_line(self) -> None:
        selected = "alpine:3.25@sha256:" + "c" * 64

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertIn("alpine:3.25", command)
            self.assertNotIn("alpine:latest", command)
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"digest": "sha256:" + "d" * 64}), ""
            )

        self.assertEqual(
            resolve_alpine_parent(selected, runner),
            "alpine:3.25@sha256:" + "d" * 64,
        )

    def test_all_platforms_resolve_before_a_failed_update_can_write(self) -> None:
        original = {
            path: path.read_bytes()
            for path in [self.dockerfile, *sorted((self.root / "packages").rglob("*.lock"))]
        }
        calls: list[tuple[str, str]] = []

        def failing(root: Path, profile: str, platform: str, parent: str, **kwargs) -> PackageLock:
            calls.append((profile, platform))
            if len(calls) == 6:
                raise RuntimeError("arm resolver failed")
            return self.resolver(root, profile, platform, parent, **kwargs)

        with self.assertRaisesRegex(RuntimeError, "arm resolver failed"):
            update_repository(
                self.root,
                write=True,
                runner=self.parent_runner,
                resolver=failing,
                publication_checker=lambda *args: True,
            )

        self.assertEqual(len(calls), 6)
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)

    def test_update_reports_changes_and_only_writes_when_requested(self) -> None:
        report = update_repository(
            self.root,
            write=False,
            runner=self.parent_runner,
            resolver=self.resolver,
            publication_checker=lambda *args: True,
        )

        self.assertEqual(report["status"], "update-available")
        self.assertEqual(len(report["changed_files"]), 7)
        self.assertEqual(read_parent(self.dockerfile.read_text()), OLD_PARENT)

        report = update_repository(
            self.root,
            write=True,
            runner=self.parent_runner,
            resolver=self.resolver,
            publication_checker=lambda *args: True,
        )

        self.assertEqual(report["status"], "update-available")
        self.assertEqual(read_parent(self.dockerfile.read_text()), NEW_PARENT)
        self.assertIn("musl=2-r0\n", (self.root / "packages/runtime/armv7.lock").read_text())

    def test_unchanged_inputs_report_pending_publication(self) -> None:
        update_repository(
            self.root,
            write=True,
            runner=self.parent_runner,
            resolver=self.resolver,
            publication_checker=lambda *args: True,
        )

        report = update_repository(
            self.root,
            write=False,
            runner=self.parent_runner,
            resolver=self.resolver,
            publication_checker=lambda *args: False,
        )

        self.assertEqual(report["status"], "pending-publication")
        self.assertTrue(report["should_build"])

    def test_image_input_digest_covers_build_inputs_but_ignores_readme(self) -> None:
        first = image_inputs_digest(self.root)
        (self.root / "README.md").write_text("documentation only\n")
        self.assertEqual(image_inputs_digest(self.root), first)
        (self.root / "root/etc/example").write_text("changed runtime\n")
        self.assertNotEqual(image_inputs_digest(self.root), first)

    def test_image_input_digest_covers_executable_mode(self) -> None:
        script = self.root / "root/etc/example"
        script.chmod(0o644)
        before = image_inputs_digest(self.root)

        script.chmod(0o755)

        self.assertNotEqual(image_inputs_digest(self.root), before)

    def test_image_input_digest_covers_dangling_symlink_identity(self) -> None:
        link = self.root / "root/etc/runtime-link"
        link.symlink_to("first-target")
        before = image_inputs_digest(self.root)

        link.unlink()
        link.symlink_to("second-target")

        self.assertNotEqual(image_inputs_digest(self.root), before)

    def test_image_input_digest_includes_new_files_supplied_as_overrides(self) -> None:
        missing = self.root / "packages/runtime/new-architecture.lock"
        without_new_lock = image_inputs_digest(self.root)

        with_new_lock = image_inputs_digest(self.root, {missing: "new lock\n"})
        missing.write_text("new lock\n", encoding="utf-8")

        self.assertNotEqual(with_new_lock, without_new_lock)
        self.assertEqual(with_new_lock, image_inputs_digest(self.root))

    def test_publication_requires_matching_labels_on_every_platform(self) -> None:
        labels = {
            "io.saltydk.apk-locks.sha256": "c" * 64,
            "io.saltydk.image-inputs.sha256": "d" * 64,
        }
        metadata = {
            "image": {
                platform: {"config": {"Labels": labels}}
                for platform in PLATFORMS
            }
        }

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")

        self.assertTrue(published_matches("image:latest", "c" * 64, "d" * 64, runner))
        metadata["image"]["linux/arm/v7"]["config"]["Labels"] = {**labels, "io.saltydk.image-inputs.sha256": "e" * 64}
        self.assertFalse(published_matches("image:latest", "c" * 64, "d" * 64, runner))

    def test_publication_inspection_fails_closed_on_transport_errors(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "TLS handshake timeout")

        with self.assertRaisesRegex(InputError, "TLS handshake timeout"):
            published_matches("image:latest", "c" * 64, "d" * 64, runner)

    def test_publication_treats_exact_registry_not_found_as_absent(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command, 1, "", "ERROR: docker.io/saltydk/example:missing: not found"
            )

        self.assertFalse(published_matches("saltydk/example:missing", "c" * 64, "d" * 64, runner))


if __name__ == "__main__":
    unittest.main()
