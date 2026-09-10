import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/apk-lock.sh"
PARENT = "alpine:3.24@sha256:" + "a" * 64
REQUESTS_SHA256 = "b" * 64


class ApkLockHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.inventory = self.directory / "inventory"
        self.after = self.directory / "after"
        self.log = self.directory / "apk.log"
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        apk = self.bin / "apk"
        apk.write_text(textwrap.dedent("""\
            #!/bin/sh
            case "$1" in
              --print-arch)
                printf '%s\\n' "${FAKE_ARCH:-x86_64}"
                ;;
              list)
                cat "$FAKE_INVENTORY"
                ;;
              version)
                case "$3" in
                  *[!A-Za-z0-9+_.:~-]*|'') printf '%s\\n' "$3"; exit 1 ;;
                esac
                ;;
              add|upgrade)
                printf '%s\\n' "$*" >> "$FAKE_LOG"
                if [ -n "${FAKE_ADD_ERROR:-}" ]; then
                  printf '%s\\n' "$FAKE_ADD_ERROR" >&2
                  exit 1
                fi
                if [ -n "${FAKE_AFTER:-}" ]; then
                  cp "$FAKE_AFTER" "$FAKE_INVENTORY"
                fi
                ;;
              *)
                printf 'unexpected apk arguments: %s\\n' "$*" >&2
                exit 64
                ;;
            esac
            """), encoding="utf-8")
        apk.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_INVENTORY": str(self.inventory),
            "FAKE_AFTER": str(self.after),
            "FAKE_LOG": str(self.log),
            "FAKE_ARCH": "x86_64",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(HELPER), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )

    def write_lock(self, packages: str, architecture: str = "x86_64") -> Path:
        path = self.directory / "packages.lock"
        path.write_text(
            "# apk-lock: 1\n"
            f"# architecture: {architecture}\n"
            f"# parent: {PARENT}\n"
            f"# requests-sha256: {REQUESTS_SHA256}\n"
            + packages,
            encoding="utf-8",
        )
        return path

    def test_inventory_emits_sorted_exact_installed_packages(self) -> None:
        self.inventory.write_text(
            "xz-libs 5.8.4-r0\nxz 5.8.4-r0\nbash 5.3.3-r1\n",
            encoding="utf-8",
        )

        result = self.run_helper("inventory")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "bash=5.3.3-r1\nxz=5.8.4-r0\nxz-libs=5.8.4-r0\n",
        )

    def test_install_passes_every_exact_constraint_and_verifies_full_inventory(self) -> None:
        lock = self.write_lock("bash=5.3.3-r1\nxz=5.8.4-r0\n")
        self.inventory.write_text("alpine-baselayout 3.7.2-r1\n", encoding="utf-8")
        self.after.write_text("xz 5.8.4-r0\nbash 5.3.3-r1\n", encoding="utf-8")

        result = self.run_helper("install", str(lock))

        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = self.log.read_text(encoding="utf-8")
        self.assertIn("bash=5.3.3-r1", invocation)
        self.assertIn("xz=5.8.4-r0", invocation)

    def test_install_rejects_an_architecture_mismatch_before_apk_add(self) -> None:
        lock = self.write_lock("xz=5.8.4-r0\n", architecture="aarch64")
        self.inventory.write_text("xz 5.8.4-r0\n", encoding="utf-8")
        self.after.write_text("xz 5.8.4-r0\n", encoding="utf-8")

        result = self.run_helper("install", str(lock))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("architecture", result.stderr)
        self.assertFalse(self.log.exists())

    def test_install_rejects_duplicate_or_unsupported_versions_before_apk_add(self) -> None:
        for packages in ("xz=1\nxz=2\n", "xz=1;touch\n"):
            with self.subTest(packages=packages):
                lock = self.write_lock(packages)
                self.log.unlink(missing_ok=True)
                result = self.run_helper("install", str(lock))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_inherited_install_rejects_changed_or_missing_parent_packages_before_apk(self) -> None:
        self.inventory.write_text("curl 8.17.0-r0\nmusl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text("curl 8.17.0-r0\nmusl 1.2.5-r21\n", encoding="utf-8")
        for packages in (
            "curl=8.18.0-r0\nmusl=1.2.5-r21\n",
            "curl=8.17.0-r0\n",
        ):
            with self.subTest(packages=packages):
                lock = self.write_lock(packages)
                self.log.unlink(missing_ok=True)
                result = self.run_helper("install", str(lock), "inherited")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("inherited package", result.stderr)
                self.assertFalse(self.log.exists())

    def test_inherited_resolve_freezes_installed_versions_and_skips_bare_duplicates(self) -> None:
        requests = self.directory / "requested.txt"
        requests.write_text("curl\njq\n", encoding="utf-8")
        self.inventory.write_text("curl 8.17.0-r0\nmusl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text(
            "musl 1.2.5-r21\njq 1.8.1-r0\ncurl 8.17.0-r0\n", encoding="utf-8"
        )

        result = self.run_helper("resolve", str(requests), "inherited")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "curl=8.17.0-r0\njq=1.8.1-r0\nmusl=1.2.5-r21\n",
        )
        invocation = self.log.read_text(encoding="utf-8")
        self.assertIn("curl=8.17.0-r0", invocation)
        self.assertIn("musl=1.2.5-r21", invocation)
        self.assertIn(" jq", invocation)
        self.assertNotIn(" curl ", invocation)

    def test_inherited_resolve_classifies_solver_conflicts(self) -> None:
        self.environment["FAKE_ADD_ERROR"] = (
            "unable to select packages:\n  libcrypto3-3.5.8-r0:\n"
            "    breaks: world[libcrypto3=3.5.7-r0]"
        )
        requests = self.directory / "requested.txt"
        requests.write_text("jq\n", encoding="utf-8")
        self.inventory.write_text("musl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text("", encoding="utf-8")

        result = self.run_helper("resolve", str(requests), "inherited")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inherited package constraints conflict", result.stderr)

    def test_inherited_resolve_does_not_treat_an_unavailable_package_as_a_parent_conflict(self) -> None:
        self.environment["FAKE_ADD_ERROR"] = "unable to select packages: requested package is unavailable"
        requests = self.directory / "requested.txt"
        requests.write_text("not-in-repository\n", encoding="utf-8")
        self.inventory.write_text("musl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text("", encoding="utf-8")

        result = self.run_helper("resolve", str(requests), "inherited")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to resolve inherited packages", result.stderr)
        self.assertNotIn("constraints conflict", result.stderr)

    def test_inherited_resolve_requires_an_exact_world_pin_to_prove_a_parent_conflict(self) -> None:
        self.environment["FAKE_ADD_ERROR"] = "new-package conflicts: other-new-package"
        requests = self.directory / "requested.txt"
        requests.write_text("new-package\n", encoding="utf-8")
        self.inventory.write_text("musl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text("", encoding="utf-8")

        result = self.run_helper("resolve", str(requests), "inherited")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to resolve inherited packages", result.stderr)
        self.assertNotIn("constraints conflict", result.stderr)

    def test_inherited_resolve_does_not_misclassify_transport_failures(self) -> None:
        self.environment["FAKE_ADD_ERROR"] = "network unavailable"
        requests = self.directory / "requested.txt"
        requests.write_text("jq\n", encoding="utf-8")
        self.inventory.write_text("musl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text("", encoding="utf-8")

        result = self.run_helper("resolve", str(requests), "inherited")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to resolve inherited packages", result.stderr)
        self.assertNotIn("constraints conflict", result.stderr)

    def test_resolve_rejects_a_partially_valid_request_list_before_apk(self) -> None:
        requests = self.directory / "requested.txt"
        requests.write_text("jq\ninvalid>=1\n", encoding="utf-8")
        self.inventory.write_text("musl 1.2.5-r21\n", encoding="utf-8")
        self.after.write_text("musl 1.2.5-r21\n", encoding="utf-8")

        result = self.run_helper("resolve", str(requests), "inherited")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid unversioned package request", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
