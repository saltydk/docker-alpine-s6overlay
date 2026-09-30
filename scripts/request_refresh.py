#!/usr/bin/env python3
"""Reuse an active base package refresh or request one on the default branch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable


REPOSITORY = "saltydk/docker-alpine-s6overlay"
WORKFLOW = "ci.yml"
REFRESH_TITLE = "Refresh base image packages"
# Check running last so a queued refresh that starts during the lookup is reused.
ACTIVE_STATUSES = ("requested", "pending", "queued", "waiting", "in_progress")
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def request_refresh(runner: Runner = run) -> tuple[str, str]:
    """Called only by the serialized request workflow, not by individual consumers."""
    for status in ACTIVE_STATUSES:
        endpoint = (
            f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs"
            f"?branch=master&status={status}&per_page=100"
        )
        completed = runner(["gh", "api", "--paginate", "--slurp", endpoint])
        if completed.returncode:
            raise RuntimeError(f"could not inspect base refreshes: {completed.stderr.strip()}")
        pages = json.loads(completed.stdout)
        if not isinstance(pages, list):
            raise ValueError("workflow run response must be a list of pages")
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
                raise ValueError("workflow run response must contain workflow_runs")
            for workflow_run in page["workflow_runs"]:
                if not isinstance(workflow_run, dict):
                    raise ValueError("workflow run must be an object")
                if (
                    workflow_run.get("display_title") != REFRESH_TITLE
                    or workflow_run.get("head_branch") != "master"
                    or workflow_run.get("event") not in {"schedule", "workflow_dispatch"}
                    or workflow_run.get("status") not in ACTIVE_STATUSES
                ):
                    continue
                run_id = workflow_run.get("id")
                if type(run_id) is not int or run_id < 1:
                    raise ValueError("active base refresh has an invalid run ID")
                return "existing", f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"

    completed = runner([
        "gh", "workflow", "run", WORKFLOW, "--repo", REPOSITORY,
        "--ref", "master", "-f", "refresh-packages=true",
    ])
    if completed.returncode:
        raise RuntimeError(f"could not request base refresh: {completed.stderr.strip()}")
    return "requested", f"https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}"


def main() -> int:
    try:
        if not os.environ.get("GH_TOKEN"):
            raise ValueError("GH_TOKEN is required for base refresh coordination")
        decision, url = request_refresh()
        message = (
            "An existing base package refresh will serve this request."
            if decision == "existing" else "A base package refresh has been requested."
        )
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"decision={decision}\nrun-url={url}\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(f"{message} [View refresh]({url}).\n")
        print(message)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"base refresh request failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
