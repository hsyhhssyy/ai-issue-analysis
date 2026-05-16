#!/usr/bin/env python3
"""
End-to-end (E2E) automated test for AI Issue Analysis.

This script:
  1. Creates a test issue on a specified GitHub repository
     (the workflow auto-triggers via ``issues.opened`` — no manual dispatch)
  2. Waits for the corresponding GitHub Actions run to appear and complete
  3. Downloads logs, artifacts, and reads the AI analysis comment
  4. Optionally closes / cleans up the test issue

Prerequisites:
  - GitHub CLI (`gh`) installed and authenticated
  - The target repository must have the ``ai-issue-analysis.yml`` workflow set up
    with ``on: issues: types: [opened]`` trigger
  - Required permissions: issues:write, actions:read on the target repository

Usage:
  # Test on the current repo (infer owner/repo from git remote)
  python scripts/test/test_e2e.py

  # Read issue body from a file
  python scripts/test/test_e2e.py \\
    --repo owner/my-repo \\
    --body-file .temp/test-issue.md

  # Keep the test issue open after the test
  python scripts/test/test_e2e.py --no-cleanup
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 15
RUN_APPEAR_TIMEOUT_SECONDS = 60  # how long to wait for the run to appear
MAX_POLL_MINUTES = 30
MAX_POLL_ITERATIONS = (MAX_POLL_MINUTES * 60) // POLL_INTERVAL_SECONDS

DEFAULT_ISSUE_TITLE = "[自动测试] AI Issue Analysis E2E Test"
DEFAULT_ISSUE_BODY = textwrap.dedent(
    """\
    ## 测试 Issue

    这是一个由自动化测试脚本创建的 Issue，用于验证 AI Issue Analysis Action 是否正常工作。

    ### 问题描述

    我在运行项目时遇到了以下问题：

    1. 执行 `npm start` 后，终端出现错误
    2. 尝试过重新安装依赖，但问题依旧

    ### 错误日志

    ```
    Error: Cannot find module 'express'
    Require stack:
    - /app/src/server.js
    - /app/src/index.js
        at Function.Module._resolveFilename (node:internal/modules/cjs/loader:933:15)
        at Function.Module._load (node:internal/modules/cjs/loader:778:27)
        at Module.require (node:internal/modules/cjs/loader:1005:19)
        at require (node:internal/modules/cjs/helpers:102:18)
        at Object.<anonymous> (/app/src/server.js:1:17)
    ```

    Node.js v18.15.0, npm v9.5.0

    **环境信息:**
    - OS: Ubuntu 22.04
    - Node: 18.15.0
    - npm: 9.5.0
    """
)

DEFAULT_WORKFLOW_FILENAME = "ai-issue-analysis.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run_gh(
    args: list[str],
    *,
    check: bool = True,
    timeout: int = 60,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Run `gh` CLI with the given arguments and return the result."""
    cmd = ["gh"] + args
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
        input=input,
    )
    if check and result.returncode != 0:
        print(f"[ERROR] gh command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        raise RuntimeError(
            f"gh command failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result


def gh_api(
    endpoint: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the GitHub API via `gh api`."""
    args = ["api", endpoint, "--method", method]
    input_data: str | None = None
    if payload is not None:
        args.extend(["--input", "-"])
        input_data = json.dumps(payload)
    result = run_gh(args, check=True, input=input_data)
    if result.stdout.strip():
        return json.loads(result.stdout)
    return {}


def get_default_repo() -> str:
    """Try to infer the default GitHub repository from the git remote."""
    result = run_gh(["repo", "view", "--json", "nameWithOwner"], check=False)
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return data.get("nameWithOwner", "")
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        for prefix in ("https://github.com/", "git@github.com:"):
            if prefix in url:
                repo = url.split(prefix)[1].replace(".git", "")
                return repo
    return ""


def hash_digest(s: str, length: int = 8) -> str:
    """Return a short hex digest of a string for unique naming."""
    return hashlib.sha256(s.encode()).hexdigest()[:length]


def poll_action_run(
    repo: str, run_id: int, *, verbose: bool = False,
) -> dict[str, Any]:
    """Poll a GitHub Actions run until it completes."""
    print(
        f"  Polling run {run_id} (every {POLL_INTERVAL_SECONDS}s, "
        f"up to {MAX_POLL_MINUTES} min)..."
    )
    for iteration in range(1, MAX_POLL_ITERATIONS + 1):
        time.sleep(POLL_INTERVAL_SECONDS)
        run_data = gh_api(f"/repos/{repo}/actions/runs/{run_id}")
        status = run_data.get("status", "unknown")
        conclusion = run_data.get("conclusion")
        elapsed = iteration * POLL_INTERVAL_SECONDS
        if verbose:
            print(f"    [{elapsed}s] status={status}, conclusion={conclusion}")
        else:
            print(f"  [{elapsed}s] status={status}, conclusion={conclusion}")

        if status == "completed":
            return run_data

    raise TimeoutError(
        f"Action run {run_id} did not complete within {MAX_POLL_MINUTES} minutes."
    )


def find_latest_run_by_issue(
    repo: str,
    workflow_filename: str,
    after_time: str,
    *,
    max_retries: int = 6,
    retry_delay: int = 10,
) -> dict[str, Any] | None:
    """Find the most recent workflow run triggered by an issues.opened event
    that was created *after* the given ISO timestamp.
    """
    for attempt in range(1, max_retries + 1):
        print(f"  Checking for runs (attempt {attempt}/{max_retries}) ...")
        data = gh_api(
            f"/repos/{repo}/actions/workflows/{workflow_filename}/runs"
            f"?event=issues&per_page=10"
        )
        runs: list[dict[str, Any]] = data.get("workflow_runs", [])

        for run in runs:
            created_at = run.get("created_at", "")
            if created_at > after_time:
                run_id = run.get("id")
                run_url = run.get("html_url", "")
                print(f"  Found matching run #{run_id}: {run_url}")
                return run

        if attempt < max_retries:
            print(f"  No matching run yet, retrying in {retry_delay}s ...")
            time.sleep(retry_delay)

    return None


def get_latest_comment_id(
    repo: str, issue_number: int, bot_user: str = "",
) -> str | None:
    """Get the first comment on the issue from the bot user (or any comment)."""
    comments = gh_api(f"/repos/{repo}/issues/{issue_number}/comments")
    if not comments:
        return None

    for comment in comments:
        comment_user = comment.get("user", {}).get("login", "")
        if bot_user and comment_user.lower() != bot_user.lower():
            continue
        return str(comment.get("id", ""))

    return str(comments[0].get("id", "")) if comments else None


def get_comment_body(repo: str, comment_id: str) -> str:
    """Get the body of a specific comment."""
    comment = gh_api(f"/repos/{repo}/issues/comments/{comment_id}")
    return comment.get("body", "")


def get_workflow_run_jobs(repo: str, run_id: int) -> list[dict[str, Any]]:
    """Get all jobs for a specific workflow run."""
    data = gh_api(f"/repos/{repo}/actions/runs/{run_id}/jobs")
    return data.get("jobs", [])


def get_job_logs(repo: str, job_id: int) -> str:
    """Download the full log for a specific job."""
    result = run_gh(
        ["api", f"/repos/{repo}/actions/jobs/{job_id}/logs", "--include"],
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    return f"[Failed to fetch logs: {result.stderr.strip()}]"


def download_action_logs(
    repo: str, run_id: int, output_dir: Path,
) -> list[Path]:
    """Download all logs for a workflow run."""
    log_paths: list[Path] = []
    jobs = get_workflow_run_jobs(repo, run_id)
    print(f"\n  Job count: {len(jobs)}")
    for job in jobs:
        job_id = job.get("id")
        job_name = job.get("name", f"job-{job_id}")
        safe_name = "".join(
            c if c.isalnum() or c in "._- " else "_" for c in job_name
        )
        log_file = output_dir / f"{safe_name}.log"
        logs = get_job_logs(repo, job_id)
        log_file.write_text(logs, encoding="utf-8")
        log_paths.append(log_file)
        print(f"  Saved job log: {log_file} ({len(logs)} chars)")
    return log_paths


def download_artifacts(
    repo: str, run_id: int, output_dir: Path,
) -> list[Path]:
    """Download all artifacts from a workflow run using gh run download."""
    output_dir.mkdir(parents=True, exist_ok=True)
    result = run_gh(
        ["run", "download", str(run_id), "--repo", repo, "--dir", str(output_dir)],
        check=False,
    )
    if result.returncode != 0:
        print(f"  [WARNING] Failed to download artifacts: {result.stderr.strip()}")
        return []
    paths = list(output_dir.iterdir()) if output_dir.exists() else []
    print(f"  Downloaded artifacts to: {output_dir} ({len(paths)} items)")
    return paths


def clean_up_issue(repo: str, issue_number: int) -> None:
    """Close a test issue."""
    print(f"\n  Cleaning up: closing issue #{issue_number} ...")
    gh_api(
        f"/repos/{repo}/issues/{issue_number}",
        method="PATCH",
        payload={"state": "closed"},
    )
    print(f"  Issue #{issue_number} closed.")


def create_test_issue(repo: str, title: str, body: str) -> dict[str, Any]:
    """Create a test issue and return the issue data from the API."""
    print(f"  Creating test issue on {repo} ...")
    result = gh_api(
        f"/repos/{repo}/issues",
        method="POST",
        payload={"title": title, "body": body},
    )
    issue_number = result.get("number")
    issue_url = result.get("html_url", "")
    print(f"  Created issue #{issue_number}: {issue_url}")
    return result


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------
def run_e2e_test(
    *,
    repo: str,
    title: str,
    body: str,
    bot_user: str,
    cleanup: bool,
    verbose: bool,
    output_dir: Path,
) -> int:
    """Run the full E2E test and return an exit code (0 = success)."""
    # Record time before creating issue, to match the run later
    pre_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    unique_id = hash_digest(pre_time)
    title = f"{title} [{unique_id}]"

    # ---- Phase 1: Create test issue (auto-triggers workflow via issues.opened) ----
    print("\n" + "=" * 60)
    print("Phase 1: Create test issue (workflow auto-triggered via issues.opened)")
    print("=" * 60)
    issue = create_test_issue(repo, title, body)
    issue_number = issue.get("number")
    if not issue_number:
        print("[FAIL] Failed to create test issue.", file=sys.stderr)
        return 1

    try:
        # ---- Phase 2: Find the auto-triggered workflow run ----
        print("\n" + "=" * 60)
        print("Phase 2: Find auto-triggered workflow run")
        print("=" * 60)
        print(
            f"  Waiting up to {RUN_APPEAR_TIMEOUT_SECONDS}s "
            "for the workflow to start ..."
        )
        run = find_latest_run_by_issue(
            repo, DEFAULT_WORKFLOW_FILENAME, after_time=pre_time,
        )
        if not run:
            print(
                f"[FAIL] No workflow run appeared within "
                f"{RUN_APPEAR_TIMEOUT_SECONDS}s after creating the issue. "
                "Check that the workflow has 'issues.opened' trigger.",
                file=sys.stderr,
            )
            return 1

        run_id = run.get("id")

        # ---- Phase 3: Poll for completion ----
        print("\n" + "=" * 60)
        print("Phase 3: Wait for action to complete")
        print("=" * 60)
        try:
            completed_run = poll_action_run(repo, run_id, verbose=verbose)
        except TimeoutError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1

        conclusion = completed_run.get("conclusion", "unknown")
        print(f"\n  Run conclusion: {conclusion}")

        # ---- Phase 4: Download logs and artifacts ----
        print("\n" + "=" * 60)
        print("Phase 4: Download logs and artifacts")
        print("=" * 60)
        log_dir = output_dir / f"run-{run_id}-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        download_action_logs(repo, run_id, log_dir)

        artifact_dir = output_dir / f"run-{run_id}-artifacts"
        download_artifacts(repo, run_id, artifact_dir)

        # ---- Phase 5: Verify the AI comment ----
        print("\n" + "=" * 60)
        print("Phase 5: Verify AI analysis comment")
        print("=" * 60)
        print("  Waiting for comment to be posted (up to 60s)...")
        comment_id = None
        for _ in range(60):
            time.sleep(1)
            comment_id = get_latest_comment_id(repo, issue_number, bot_user=bot_user)
            if comment_id:
                break

        if comment_id:
            comment_body = get_comment_body(repo, comment_id)
            print(f"  Found comment #{comment_id} ({len(comment_body)} chars)")

            comment_file = output_dir / f"run-{run_id}-comment.md"
            comment_file.write_text(comment_body, encoding="utf-8")
            print(f"  Saved comment to: {comment_file}")

            preview = comment_body[:500].replace("\n", "\\n")
            print("\n  Comment preview (first 500 chars):")
            print(f"  {preview}...")

            if conclusion == "success" and comment_body:
                print("\n  [PASS] Action succeeded and AI posted a response.")
            else:
                print(
                    f"\n  [WARN] Action conclusion is '{conclusion}', "
                    "comment may be an error message."
                )
        else:
            print("  [WARN] No comment found from the AI bot.")

        # Collect results
        results = {
            "repo": repo,
            "issue_number": issue_number,
            "run_id": run_id,
            "conclusion": conclusion,
            "has_comment": comment_id is not None,
            "comment_length": (
                len(get_comment_body(repo, comment_id)) if comment_id else 0
            ),
        }
        result_file = output_dir / f"run-{run_id}-results.json"
        result_file.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"\n  Results saved to: {result_file}")

        print("\n" + "=" * 60)
        print(f"Overall: {'PASS' if conclusion == 'success' else 'CHECK_RESULTS'}")
        print("=" * 60)
        return 0 if conclusion == "success" else 2

    finally:
        if cleanup:
            print("\n" + "=" * 60)
            print("Cleanup: Close test issue")
            print("=" * 60)
            clean_up_issue(repo, issue_number)
        else:
            print(
                f"\n  [INFO] Skipping cleanup. "
                f"Issue #{issue_number} left open on {repo}."
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="E2E test for AI Issue Analysis GitHub Action",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repository (owner/repo). Default: inferred from git remote.",
    )
    parser.add_argument(
        "--title",
        default=DEFAULT_ISSUE_TITLE,
        help="Test issue title.",
    )
    parser.add_argument(
        "--body",
        default=DEFAULT_ISSUE_BODY,
        help="Test issue body.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        default=None,
        help="Read test issue body from a file (overrides --body).",
    )
    parser.add_argument(
        "--bot-user",
        default="",
        help="GitHub username of the AI bot (for comment filtering).",
    )
    parser.add_argument(
        "--no-cleanup",
        dest="cleanup",
        action="store_false",
        default=True,
        help="Do NOT close the test issue after the test completes.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Show detailed polling output.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".cache/e2e-test-results"),
        help="Directory to store test artifacts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    repo = args.repo or get_default_repo()
    if not repo:
        print(
            "[ERROR] Could not determine repository. "
            "Use --repo owner/repo or run from a git repo with a GitHub remote.",
            file=sys.stderr,
        )
        return 1
    print(f"Target repository: {repo}")

    body = args.body
    if args.body_file:
        if args.body_file.is_file():
            body = args.body_file.read_text(encoding="utf-8")
        else:
            print(f"[ERROR] Body file not found: {args.body_file}", file=sys.stderr)
            return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    return run_e2e_test(
        repo=repo,
        title=args.title,
        body=body,
        bot_user=args.bot_user,
        cleanup=args.cleanup,
        verbose=args.verbose,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
