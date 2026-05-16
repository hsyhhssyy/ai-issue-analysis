#!/usr/bin/env python3
"""
End-to-end (E2E) automated test for AI Issue Analysis.

This script:
  1. Creates a test issue on a specified GitHub repository
  2. Triggers a workflow_dispatch to run the AI analysis on that issue
  3. Polls the GitHub Actions run until it completes
  4. Reads the AI's analysis comment reply
  5. Optionally closes / cleans up the test issue

Prerequisites:
  - GitHub CLI (`gh`) installed and authenticated
  - The target repository must have the `ai-issue-analysis.yml` workflow set up
  - Required permissions: issues:write, actions:read on the target repository

Usage:
  # Basic: test on the current repo (infer owner/repo from git remote)
  python scripts/test/test_e2e.py

  # Specify a target repository and issue title/body
  python scripts/test/test_e2e.py \
    --repo owner/my-repo \
    --title "测试: 应用启动报错" \
    --body "启动时出现以下错误：\n\n```\nError: Cannot find module 'express'\n```\n\n请问如何解决？" \
    --workflow ai-issue-analysis.yml

  # Keep the test issue open after the test (no cleanup)
  python scripts/test/test_e2e.py --no-cleanup

  # Output full action run logs
  python scripts/test/test_e2e.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 15
MAX_POLL_MINUTES = 30
MAX_POLL_ITERATIONS = (MAX_POLL_MINUTES * 60) // POLL_INTERVAL_SECONDS

DEFAULT_ISSUE_TITLE = "[自动测试] AI Issue Analysis E2E Test"
DEFAULT_ISSUE_BODY = textwrap.dedent(
    """\
    ## 测试 Issue

    这是一个由自动化测试脚本创建的 Issue，用于验证 AI Issue Analysis Action 是否正常工作。

    ### 问题描述

    我在运行项目时遇到了以下问题：

    1. 执行 `npm start` 后，终端输出以下错误：
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
def run_gh(args: list[str], *, check: bool = True, timeout: int = 60, input: str | None = None) -> subprocess.CompletedProcess:
    """Run `gh` CLI with the given arguments and return the result."""
    cmd = ["gh"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, input=input)
    if check and result.returncode != 0:
        print(f"[ERROR] gh command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        raise RuntimeError(f"gh command failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def gh_api(endpoint: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
    # Fallback: try parsing git remote
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


def poll_action_run(repo: str, run_id: int, *, verbose: bool = False) -> dict[str, Any]:
    """Poll a GitHub Actions run until it completes.

    Returns the concluded run data from the API.
    """
    print(f"  Polling run {run_id} (every {POLL_INTERVAL_SECONDS}s, up to {MAX_POLL_MINUTES} min)...")
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


def get_latest_comment_id(repo: str, issue_number: int, bot_user: str = "") -> str | None:
    """Get the first comment on the issue (presumably the AI's comment) from a bot user."""
    comments = gh_api(f"/repos/{repo}/issues/{issue_number}/comments")
    if not comments:
        return None

    for comment in comments:
        comment_user = comment.get("user", {}).get("login", "")
        if bot_user and comment_user.lower() != bot_user.lower():
            continue
        return str(comment.get("id", ""))

    # If no bot_user filter, just return the first comment
    return str(comments[0].get("id", "")) if comments else None


def get_comment_body(repo: str, comment_id: str) -> str:
    """Get the body of a specific comment."""
    comment = gh_api(f"/repos/{repo}/issues/comments/{comment_id}")
    return comment.get("body", "")


def list_workflow_runs(repo: str, workflow_filename: str, event: str = "workflow_dispatch", per_page: int = 5) -> list[dict[str, Any]]:
    """List recent workflow runs for a given workflow file."""
    data = gh_api(
        f"/repos/{repo}/actions/workflows/{workflow_filename}/runs"
        f"?event={event}&per_page={per_page}"
    )
    return data.get("workflow_runs", [])


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


def download_action_logs(repo: str, run_id: int, output_dir: Path) -> list[Path]:
    """Download all logs for a workflow run.

    Returns a list of saved log file paths.
    """
    log_paths: list[Path] = []
    jobs = get_workflow_run_jobs(repo, run_id)
    print(f"\n  Job count: {len(jobs)}")
    for job in jobs:
        job_id = job.get("id")
        job_name = job.get("name", f"job-{job_id}")
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in job_name)
        log_file = output_dir / f"{safe_name}.log"
        logs = get_job_logs(repo, job_id)
        log_file.write_text(logs, encoding="utf-8")
        log_paths.append(log_file)
        print(f"  Saved job log: {log_file} ({len(logs)} chars)")
    return log_paths


def download_artifacts(repo: str, run_id: int, output_dir: Path) -> list[Path]:
    """Download all artifacts from a workflow run using gh run download.

    Returns a list of directories/files downloaded.
    """
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
    """Create a test issue on the repository."""
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


def trigger_workflow_dispatch(
    repo: str,
    workflow_filename: str,
    issue_number: int,
    ref: str = "main",
) -> None:
    """Trigger a workflow_dispatch to analyze the given issue."""
    print(f"  Triggering workflow '{workflow_filename}' on {repo} (ref: {ref}) ...")
    run_gh([
        "workflow", "run", workflow_filename,
        "--repo", repo,
        "--ref", ref,
        "--field", f"issue_number={issue_number}",
    ])
    print("  Workflow triggered. Waiting a few seconds for the run to appear ...")


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------
def run_e2e_test(
    *,
    repo: str,
    title: str,
    body: str,
    workflow_filename: str,
    ref: str,
    bot_user: str,
    cleanup: bool,
    verbose: bool,
    output_dir: Path,
) -> int:
    """Run the full E2E test and return an exit code (0 = success)."""
    # ---- Phase 1: Create test issue ----
    print("\n" + "=" * 60)
    print("Phase 1: Create test issue")
    print("=" * 60)
    issue = create_test_issue(repo, title, body)
    issue_number = issue.get("number")
    if not issue_number:
        print("[FAIL] Failed to create test issue.", file=sys.stderr)
        return 1

    try:
        # ---- Phase 2: Trigger workflow ----
        print("\n" + "=" * 60)
        print("Phase 2: Trigger workflow_dispatch")
        print("=" * 60)
        trigger_workflow_dispatch(repo, workflow_filename, issue_number, ref=ref)

        # ---- Phase 3: Find the triggered run ----
        print("\n" + "=" * 60)
        print("Phase 3: Find triggered workflow run")
        print("=" * 60)
        time.sleep(10)  # Give GitHub a moment to register the run
        runs = list_workflow_runs(repo, workflow_filename, event="workflow_dispatch")
        if not runs:
            print("[FAIL] No workflow runs found after trigger.", file=sys.stderr)
            return 1

        latest_run = runs[0]
        run_id = latest_run.get("id")
        run_url = latest_run.get("html_url", "")
        print(f"  Found run #{run_id}: {run_url}")

        # ---- Phase 4: Poll for completion ----
        print("\n" + "=" * 60)
        print("Phase 4: Wait for action to complete")
        print("=" * 60)
        try:
            completed_run = poll_action_run(repo, run_id, verbose=verbose)
        except TimeoutError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1

        conclusion = completed_run.get("conclusion", "unknown")
        print(f"\n  Run conclusion: {conclusion}")

        # ---- Phase 5: Download logs and artifacts ----
        print("\n" + "=" * 60)
        print("Phase 5: Download logs and artifacts")
        print("=" * 60)
        log_dir = output_dir / f"run-{run_id}-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        download_action_logs(repo, run_id, log_dir)

        artifact_dir = output_dir / f"run-{run_id}-artifacts"
        download_artifacts(repo, run_id, artifact_dir)

        # ---- Phase 6: Verify the AI comment ----
        print("\n" + "=" * 60)
        print("Phase 6: Verify AI analysis comment")
        print("=" * 60)
        print("  Waiting for comment to be posted (up to 60s)...")
        comment_id = None
        for wait_second in range(1, 61):
            time.sleep(1)
            comment_id = get_latest_comment_id(repo, issue_number, bot_user=bot_user)
            if comment_id:
                break

        if comment_id:
            comment_body = get_comment_body(repo, comment_id)
            print(f"  Found comment #{comment_id} ({len(comment_body)} chars)")

            # Save comment for inspection
            comment_file = output_dir / f"run-{run_id}-comment.md"
            comment_file.write_text(comment_body, encoding="utf-8")
            print(f"  Saved comment to: {comment_file}")

            # Print preview
            preview = comment_body[:500].replace("\n", "\\n")
            print(f"\n  Comment preview (first 500 chars):")
            print(f"  {preview}...")

            if conclusion == "success" and comment_body:
                print("\n  [PASS] Action completed successfully and AI posted a response.")
            else:
                print(f"\n  [WARN] Action conclusion is '{conclusion}', comment may be an error message.")
        else:
            print("  [WARN] No comment found from the AI bot.")
            print("  The action may have failed before posting a comment.")

        # Collect results
        results = {
            "repo": repo,
            "issue_number": issue_number,
            "run_id": run_id,
            "conclusion": conclusion,
            "has_comment": comment_id is not None,
            "comment_length": len(get_comment_body(repo, comment_id)) if comment_id else 0,
        }
        result_file = output_dir / f"run-{run_id}-results.json"
        result_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  Results saved to: {result_file}")

        print("\n" + "=" * 60)
        print(f"Overall: {'PASS' if conclusion == 'success' else 'CHECK_RESULTS'}")
        print("=" * 60)
        return 0 if conclusion == "success" else 2

    finally:
        # ---- Cleanup ----
        if cleanup:
            print("\n" + "=" * 60)
            print("Cleanup: Close test issue")
            print("=" * 60)
            clean_up_issue(repo, issue_number)
        else:
            print(f"\n  [INFO] Skipping cleanup. Issue #{issue_number} left open on {repo}.")


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
        help=f"Test issue title. Default: '{DEFAULT_ISSUE_TITLE}'",
    )
    parser.add_argument(
        "--body",
        default=DEFAULT_ISSUE_BODY,
        help="Test issue body. Default: a sample Node.js error report.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        default=None,
        help="Read test issue body from a file (overrides --body).",
    )
    parser.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW_FILENAME,
        help=f"Workflow filename. Default: {DEFAULT_WORKFLOW_FILENAME}",
    )
    parser.add_argument(
        "--ref",
        default="main",
        help="Git ref (branch/tag) to trigger the workflow on. Default: main",
    )
    parser.add_argument(
        "--bot-user",
        default="",
        help="GitHub username of the AI bot (for comment filtering). "
        "If empty, returns the first comment on the issue.",
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
        help="Directory to store test artifacts and logs. Default: .cache/e2e-test-results",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Resolve repository
    repo = args.repo or get_default_repo()
    if not repo:
        print(
            "[ERROR] Could not determine repository. "
            "Use --repo owner/repo or run this script from a git repo with a GitHub remote.",
            file=sys.stderr,
        )
        return 1
    print(f"Target repository: {repo}")

    # Resolve issue body
    body = args.body
    if args.body_file:
        if args.body_file.is_file():
            body = args.body_file.read_text(encoding="utf-8")
        else:
            print(f"[ERROR] Body file not found: {args.body_file}", file=sys.stderr)
            return 1

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Run test
    return run_e2e_test(
        repo=repo,
        title=args.title,
        body=body,
        workflow_filename=args.workflow,
        ref=args.ref,
        bot_user=args.bot_user,
        cleanup=args.cleanup,
        verbose=args.verbose,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
