import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.request_refresh import ACTIVE_STATUSES, REFRESH_TITLE, main, request_refresh


def active_refresh(status="queued", **overrides):
    return {"id": 123, "display_title": REFRESH_TITLE, "head_branch": "master",
            "event": "workflow_dispatch", "status": status, **overrides}


class FakeGitHub:
    def __init__(self, runs=()):
        self.runs = list(runs)
        self.calls = []
        self.dispatches = 0
        self.read_error = ""
        self.dispatch_error = ""

    def __call__(self, command):
        self.calls.append(command)
        if command[1] == "api":
            if self.read_error:
                return subprocess.CompletedProcess(command, 1, "", self.read_error)
            status = command[-1].split("status=", 1)[1].split("&", 1)[0]
            runs = [run for run in self.runs if run["status"] == status]
            # The matching run may be on a later page.
            return subprocess.CompletedProcess(command, 0, json.dumps([
                {"workflow_runs": []}, {"workflow_runs": runs},
            ]), "")
        self.dispatches += 1
        if self.dispatch_error:
            return subprocess.CompletedProcess(command, 1, "", self.dispatch_error)
        self.runs.append(active_refresh())
        return subprocess.CompletedProcess(command, 0, "", "")


class RequestRefreshTests(unittest.TestCase):
    def test_reuses_each_active_refresh_state_without_dispatching(self):
        for status in ACTIVE_STATUSES:
            for event in ("schedule", "workflow_dispatch"):
                with self.subTest(status=status, event=event):
                    github = FakeGitHub([active_refresh(status, event=event)])
                    decision, url = request_refresh(github)
                    self.assertEqual(decision, "existing")
                    self.assertTrue(url.endswith("/actions/runs/123"))
                    self.assertEqual(github.dispatches, 0)

    def test_only_refreshes_on_the_base_default_branch_are_reused(self):
        github = FakeGitHub([
            active_refresh(event="push"), active_refresh(event="pull_request"),
            active_refresh(display_title="Build base image"), active_refresh(head_branch="feature"),
            active_refresh(status="completed"),
        ])
        decision, _ = request_refresh(github)
        self.assertEqual(decision, "requested")
        self.assertEqual(github.dispatches, 1)
        self.assertEqual(github.calls[-1], [
            "gh", "workflow", "run", "ci.yml", "--repo", "saltydk/docker-alpine-s6overlay",
            "--ref", "master", "-f", "refresh-packages=true",
        ])

    def test_serial_consumer_requests_share_one_queued_refresh(self):
        github = FakeGitHub()
        self.assertEqual(request_refresh(github)[0], "requested")
        self.assertEqual(request_refresh(github)[0], "existing")
        self.assertEqual(github.dispatches, 1)

    def test_queued_refresh_that_starts_during_lookup_is_reused(self):
        github = FakeGitHub([active_refresh()])
        def runner(command):
            result = github(command)
            github.runs[0]["status"] = "in_progress"
            return result
        self.assertEqual(request_refresh(runner)[0], "existing")
        self.assertEqual(github.dispatches, 0)

    def test_lookup_failure_does_not_dispatch_blindly(self):
        github = FakeGitHub()
        github.read_error = "HTTP 403 Forbidden"
        with self.assertRaisesRegex(RuntimeError, "could not inspect"):
            request_refresh(github)
        self.assertEqual(github.dispatches, 0)

    def test_invalid_lookup_response_does_not_dispatch(self):
        for value in ({}, [{}], [{"workflow_runs": [None]}], [{"workflow_runs": [active_refresh(id=True)]}]):
            with self.subTest(value=value):
                calls = []
                def runner(command):
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
                with self.assertRaises(ValueError):
                    request_refresh(runner)
                self.assertTrue(all(command[1] == "api" for command in calls))

    def test_rejected_dispatch_is_an_error(self):
        github = FakeGitHub()
        github.dispatch_error = "HTTP 422 Workflow disabled"
        with self.assertRaisesRegex(RuntimeError, "could not request"):
            request_refresh(github)
        self.assertEqual(github.dispatches, 1)

    def test_main_requires_credentials_before_requesting(self):
        with patch.dict(os.environ, {"GH_TOKEN": ""}), \
                patch("scripts.request_refresh.request_refresh") as request, redirect_stderr(StringIO()):
            self.assertEqual(main(), 1)
            request.assert_not_called()

    def test_main_reports_the_reused_run(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            summary = Path(directory) / "summary"
            url = "https://github.com/saltydk/docker-alpine-s6overlay/actions/runs/123"
            with patch.dict(os.environ, {"GH_TOKEN": "fixture", "GITHUB_OUTPUT": str(output),
                                         "GITHUB_STEP_SUMMARY": str(summary)}), \
                    patch("scripts.request_refresh.request_refresh", return_value=("existing", url)), \
                    redirect_stdout(StringIO()):
                self.assertEqual(main(), 0)
            self.assertIn("decision=existing", output.read_text())
            self.assertIn(url, summary.read_text())


if __name__ == "__main__":
    unittest.main()
