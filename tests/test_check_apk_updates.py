import subprocess
import unittest

from scripts.check_apk_updates import UpdateCheckError, check_platforms, parse_apk_updates


class ParseApkUpdatesTests(unittest.TestCase):
    def test_extracts_only_upgrade_rows(self) -> None:
        output = """Installed:                                Available:
apk-tools-3.0.6-r0                      < 3.0.8-r0
busybox-1.37.0-r31                      = 1.37.0-r31
python3-3.14.5-r0                       < 3.14.7-r1
"""

        self.assertEqual(
            parse_apk_updates(output),
            (
                "apk-tools-3.0.6-r0 < 3.0.8-r0",
                "python3-3.14.5-r0 < 3.14.7-r1",
            ),
        )


class CheckPlatformsTests(unittest.TestCase):
    def test_reports_refresh_when_any_platform_has_updates(self) -> None:
        outputs = {
            "linux/amd64": "curl-8.21.0-r0 < 8.22.0-r0\n",
            "linux/arm64": "",
        }

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            platform = command[command.index("--platform") + 1]
            return subprocess.CompletedProcess(command, 0, outputs[platform], "")

        report = check_platforms(
            "saltydk/alpine-s6overlay:latest",
            ("linux/amd64", "linux/arm64"),
            runner,
        )

        self.assertTrue(report["needs_refresh"])
        self.assertEqual(report["platforms"][0]["updates"], ["curl-8.21.0-r0 < 8.22.0-r0"])
        self.assertEqual(report["platforms"][1]["updates"], [])

    def test_reports_no_refresh_when_every_platform_is_current(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "Installed: Available:\n", "")

        report = check_platforms(
            "saltydk/alpine-s6overlay:latest",
            ("linux/amd64", "linux/arm64", "linux/arm/v7"),
            runner,
        )

        self.assertFalse(report["needs_refresh"])
        self.assertEqual([item["platform"] for item in report["platforms"]], [
            "linux/amd64",
            "linux/arm64",
            "linux/arm/v7",
        ])

    def test_fails_closed_when_a_platform_probe_fails(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 125, "", "qemu failed")

        with self.assertRaisesRegex(UpdateCheckError, "linux/arm64.*qemu failed"):
            check_platforms(
                "saltydk/alpine-s6overlay:latest",
                ("linux/arm64",),
                runner,
            )


if __name__ == "__main__":
    unittest.main()
