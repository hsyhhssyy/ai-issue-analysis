#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path


MAX_OUTPUT_BYTES = 300000
DEFAULT_API_TIMEOUT = 60


def normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: Path, missing_message: str = "") -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return missing_message


def append_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(value)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def write_output(name: str, value: str, github_output: Path) -> None:
    delimiter = f"EOF_{uuid.uuid4().hex}"
    normalized_value = value if value.endswith("\n") else f"{value}\n"
    with github_output.open("a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n")
        handle.write(normalized_value)
        handle.write(f"{delimiter}\n")


def truncate_for_output(value: str, label: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    suffix = (
        f"\n\n[Truncated {label} for GitHub Actions output size limits. "
        "Use the uploaded artifact for the full content.]\n"
    )
    suffix_bytes = suffix.encode("utf-8")
    trimmed = encoded[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{trimmed}{suffix}"


def github_request(
    api_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    url = f"{api_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-issue-analysis",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_API_TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed: {method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {method} {path} -> {exc}") from exc

    if not body.strip():
        return {}
    return json.loads(body)


class ActionRunner:
    def __init__(self) -> None:
        self.workspace_root = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
        self.action_path = Path(os.environ["GITHUB_ACTION_PATH"]).resolve()
        self.github_output = Path(os.environ["GITHUB_OUTPUT"])
        self.event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        self.api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        self.repository = os.environ["GITHUB_REPOSITORY"]
        self.run_id = os.environ.get("GITHUB_RUN_ID", "")
        self.event_name = os.environ.get("GITHUB_EVENT_NAME", "")

        self.input_issue_number = os.environ.get("INPUT_ISSUE_NUMBER", "").strip()
        self.github_token = os.environ.get("INPUT_GITHUB_TOKEN", "").strip()
        self.copilot_github_token = os.environ.get("INPUT_COPILOT_GITHUB_TOKEN", "")
        self.bot_name = os.environ.get("INPUT_BOT_NAME", "")
        self.initial_comment_body = os.environ.get("INPUT_INITIAL_COMMENT_BODY", "")
        self.action_link_text = os.environ.get("INPUT_ACTION_LINK_TEXT", "GitHub Action 运行记录")
        self.details_summary = os.environ.get("INPUT_DETAILS_SUMMARY", "点击此处展开分析过程")
        self.prompt_template = os.environ.get("INPUT_PROMPT_TEMPLATE", "")
        self.comment_prompt_template = os.environ.get("INPUT_COMMENT_PROMPT_TEMPLATE", "")
        self.llm_config_json = os.environ.get("INPUT_LLM_CONFIG_JSON", "")
        self.litellm_package = os.environ.get("INPUT_LITELLM_PACKAGE", "litellm")
        self.analysis_max_iterations = int(os.environ.get("INPUT_ANALYSIS_MAX_ITERATIONS", "12"))
        self.copilot_model = os.environ.get("INPUT_COPILOT_MODEL", "gpt-5.4")
        self.copilot_reasoning_effort = os.environ.get("INPUT_COPILOT_REASONING_EFFORT", "xhigh")
        self.stream_update_interval = max(1, int(os.environ.get("INPUT_STREAM_UPDATE_INTERVAL_SECONDS", "30")))
        self.cache_dir = self.resolve_workspace_path(os.environ.get("INPUT_CACHE_DIR", ".cache"))
        self.copilot_answer_file_raw = os.environ.get("INPUT_COPILOT_ANSWER_FILE", "copilot_answer.md")
        self.copilot_answer_file = self.resolve_workspace_path(self.copilot_answer_file_raw, create_parent=True)
        self.copilot_package = os.environ.get("INPUT_COPILOT_PACKAGE", "@github/copilot")
        self.process_error_message = os.environ.get("INPUT_PROCESS_ERROR_MESSAGE", "分析过程出现错误，请重试。")
        self.result_error_message = os.environ.get("INPUT_RESULT_ERROR_MESSAGE", "分析结果出现错误，请重试。")
        self.extra_comment_content = os.environ.get("INPUT_EXTRA_COMMENT_CONTENT", "")

        self.analysis_prompt_file = self.cache_dir / "analysis_prompt.txt"
        self.copilot_output_file = self.cache_dir / "copilot_output.log"
        self.copilot_execution_log_file = self.cache_dir / "copilot_execution.log"
        self.copilot_output_artifact_file = self.cache_dir / "copilot_output.txt"
        self.final_comment_file = self.cache_dir / "final_comment.md"
        self.final_conclusion_artifact_file = self.cache_dir / "final_conclusion.md"
        self.litellm_venv_dir = self.cache_dir / "litellm-venv"
        self.litellm_python = self.litellm_venv_dir / "bin" / "python"
        self.run_litellm_script = self.action_path / "scripts" / "run_litellm_analysis.py"

        self.details_begin = f"<details><summary>{self.details_summary}</summary>"
        self.details_end = "</details>"
        self.action_link = f"🔗 [{self.action_link_text}]({self.server_url}/{self.repository}/actions/runs/{self.run_id})"

        self.event_payload = json.loads(self.event_path.read_text(encoding="utf-8"))
        self.issue_number = ""
        self.comment_id = ""
        self.comment_url = ""
        self.final_conclusion = ""
        self.failure_message = ""
        self.analysis_success = False

    def resolve_workspace_path(self, path_value: str, *, create_parent: bool = False) -> Path:
        candidate = (self.workspace_root / (path_value.strip() or ".")).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise RuntimeError(f"Path escapes workspace root: {path_value}")
        if create_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def log(self, message: str = "") -> None:
        line = f"{message}\n"
        append_text(self.copilot_execution_log_file, line)
        print(message, flush=True)

    def github_issue_comment_path(self) -> str:
        return f"/repos/{self.repository}/issues/comments/{self.comment_id}"

    def prepare_files(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        write_text(self.copilot_output_file, "")
        write_text(self.copilot_execution_log_file, "")

    def determine_issue_number(self) -> str:
        if self.input_issue_number:
            return self.input_issue_number

        if self.event_name in {"issues", "issue_comment"}:
            issue_number = self.event_payload.get("issue", {}).get("number")
            if issue_number is not None:
                return str(issue_number)

        if self.event_name == "workflow_dispatch":
            issue_number = str(self.event_payload.get("inputs", {}).get("issue_number", "")).strip()
            if issue_number:
                return issue_number

        raise RuntimeError("Unable to determine issue number. Pass input issue-number or expose workflow_dispatch input issue_number.")

    def build_prompt(self) -> str:
        raw_comment = ""
        if self.event_name == "issue_comment":
            raw_comment = self.event_payload.get("comment", {}).get("body", "")

        cleaned_comment = raw_comment
        if self.bot_name:
            cleaned_comment = re.sub(rf"{re.escape(self.bot_name)}\\b", "", cleaned_comment, flags=re.IGNORECASE)
        cleaned_comment = re.sub(r"^[\s,，.。!！?？:：]+", "", cleaned_comment).strip()

        def render(template: str) -> str:
            return (
                template
                .replace("{{issue_number}}", self.issue_number)
                .replace("{{copilot_answer_file}}", self.copilot_answer_file_raw)
                .replace("{{comment_body}}", cleaned_comment)
                .replace("{{repository}}", self.repository)
                .replace("{{event_name}}", self.event_name)
            )

        prompt = render(self.prompt_template).strip()
        extra_prompt = render(self.comment_prompt_template).strip() if cleaned_comment else ""
        if extra_prompt:
            prompt = f"{prompt}\n\n{extra_prompt}" if prompt else extra_prompt

        write_text(self.analysis_prompt_file, f"{prompt}\n")
        return prompt

    def create_initial_comment(self) -> None:
        body = f"{self.initial_comment_body.rstrip()}\n---\n{self.action_link}"
        response = github_request(
            self.api_url,
            f"/repos/{self.repository}/issues/{self.issue_number}/comments",
            self.github_token,
            method="POST",
            payload={"body": body},
        )
        self.comment_id = str(response.get("id", ""))
        self.comment_url = str(response.get("html_url", ""))
        if not self.comment_id:
            raise RuntimeError("Created issue comment did not return an id.")
        self.log(f"Created comment id '{self.comment_id}' on issue '{self.issue_number}'.")

    def update_comment(self, body: str) -> None:
        if not self.comment_id:
            return
        github_request(
            self.api_url,
            self.github_issue_comment_path(),
            self.github_token,
            method="PATCH",
            payload={"body": body},
        )

    def build_stream_comment(self, current_content: str) -> str:
        parts = [
            self.initial_comment_body.rstrip(),
            "---",
            self.details_begin,
            "",
            "```text",
            current_content.rstrip(),
            "```",
            "",
            self.details_end,
            "",
            self.action_link,
        ]
        if self.extra_comment_content:
            parts.extend(["", self.extra_comment_content])
        return "\n".join(parts).rstrip() + "\n"

    def build_final_comment(self) -> str:
        live_output = read_text(self.copilot_output_file)
        final_conclusion = self.final_conclusion or self.result_error_message
        parts = [
            final_conclusion.rstrip(),
            "---",
            self.details_begin,
            "",
            "```text",
            live_output.rstrip(),
            "```",
            "",
            self.details_end,
            "",
            self.action_link,
        ]
        if self.extra_comment_content:
            parts.extend(["", self.extra_comment_content])
        return "\n".join(parts).rstrip() + "\n"

    def run_command(self, command: list[str], description: str, *, env: dict[str, str] | None = None) -> None:
        result = subprocess.run(
            command,
            cwd=self.workspace_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip():
            self.log(result.stdout.rstrip())
        if result.stderr.strip():
            self.log(result.stderr.rstrip())
        if result.returncode != 0:
            raise RuntimeError(f"{description} failed with exit code {result.returncode}.")

    def install_litellm(self) -> None:
        self.log(f"Installing LiteLLM package: {self.litellm_package}")
        self.run_command(["python3", "-m", "venv", str(self.litellm_venv_dir)], "Create LiteLLM virtualenv")
        self.run_command(
            [str(self.litellm_python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--upgrade", "pip"],
            "Upgrade pip",
        )
        self.run_command(
            [str(self.litellm_python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", self.litellm_package],
            "Install LiteLLM",
        )

    def install_copilot_cli(self) -> None:
        self.log(f"Installing Copilot CLI package: {self.copilot_package}")
        self.run_command(["npm", "install", "-g", self.copilot_package], "Install Copilot CLI")

    def select_copilot_token(self) -> tuple[str, int]:
        tokens = [line.strip() for line in self.copilot_github_token.splitlines() if line.strip()]
        if not tokens:
            raise RuntimeError("Input copilot-github-token is empty after trimming blank lines.")
        return secrets.choice(tokens), len(tokens)

    def choose_analysis_path(self) -> str:
        if self.llm_config_json.strip():
            return "litellm"
        if self.copilot_github_token.strip():
            return "copilot"
        raise RuntimeError(
            "Neither llm-config-json nor copilot-github-token was provided. Pass llm-config-json for LiteLLM or copilot-github-token for Copilot CLI fallback."
        )

    def run_process_with_streaming(self, command: list[str], env: dict[str, str]) -> int:
        with self.copilot_output_file.open("w", encoding="utf-8") as handle:
            process = subprocess.Popen(
                command,
                cwd=self.workspace_root,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )

        last_content = ""
        while True:
            return_code = process.poll()
            current_content = read_text(self.copilot_output_file)
            if current_content and current_content != last_content:
                try:
                    self.update_comment(self.build_stream_comment(current_content))
                    self.log(f"Comment updated at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                except Exception as exc:
                    self.log(f"Warning: failed to stream comment update: {exc}")
                last_content = current_content
            if return_code is not None:
                return return_code
            time.sleep(self.stream_update_interval)

    def append_process_output(self, heading: str) -> None:
        live_output = read_text(self.copilot_output_file)
        block = ["", f"{heading} begins"]
        if live_output:
            block.append(live_output.rstrip())
        block.extend(["", f"{heading} ends"])
        append_text(self.copilot_execution_log_file, "\n".join(block) + "\n")

    def run_litellm(self) -> int:
        self.install_litellm()
        self.log("LiteLLM invocation parameters:")
        self.log(f"  repo: {self.repository}")
        self.log(f"  issue-number: {self.issue_number}")
        self.log(f"  comment-id: {self.comment_id}")
        self.log(f"  comment-url: {self.comment_url}")
        self.log(f"  llm-config-json-length: {len(self.llm_config_json)}")
        self.log(f"  max-iterations: {self.analysis_max_iterations}")
        self.log(f"  stream-update-interval-seconds: {self.stream_update_interval}")
        self.log(f"  analysis-prompt-file: {self.analysis_prompt_file}")
        self.log(f"  output-file: {self.copilot_output_file}")
        self.log(f"  answer-file: {self.copilot_answer_file}")
        self.log(f"  litellm-python: {self.litellm_python}")
        self.log(
            "  command: "
            f"{self.litellm_python} {self.run_litellm_script} --llm-config-json '<redacted>' "
            f"--analysis-prompt-file \"{self.analysis_prompt_file}\" --answer-file \"{self.copilot_answer_file}\" "
            f"--repo \"{self.repository}\" --issue-number \"{self.issue_number}\" --github-token '<redacted>' "
            f"--max-iterations \"{self.analysis_max_iterations}\""
        )
        self.log("Prompt content begins")
        self.log(read_text(self.analysis_prompt_file).rstrip())
        self.log("Prompt content ends")

        command = [
            str(self.litellm_python),
            str(self.run_litellm_script),
            "--llm-config-json",
            self.llm_config_json,
            "--analysis-prompt-file",
            str(self.analysis_prompt_file),
            "--answer-file",
            str(self.copilot_answer_file),
            "--repo",
            self.repository,
            "--issue-number",
            self.issue_number,
            "--github-token",
            self.github_token,
            "--max-iterations",
            str(self.analysis_max_iterations),
        ]
        return_code = self.run_process_with_streaming(command, os.environ.copy())
        self.append_process_output("Analysis output")
        return return_code

    def run_copilot(self) -> int:
        copilot_token, token_count = self.select_copilot_token()
        self.install_copilot_cli()
        prompt = read_text(self.analysis_prompt_file)

        self.log("Copilot invocation parameters:")
        self.log(f"  repo: {self.repository}")
        self.log(f"  issue-number: {self.issue_number}")
        self.log(f"  comment-id: {self.comment_id}")
        self.log(f"  comment-url: {self.comment_url}")
        self.log(f"  model: {self.copilot_model}")
        self.log(f"  reasoning-effort: {self.copilot_reasoning_effort}")
        self.log(f"  copilot-token-count: {token_count}")
        self.log(f"  stream-update-interval-seconds: {self.stream_update_interval}")
        self.log(f"  analysis-prompt-file: {self.analysis_prompt_file}")
        self.log(f"  output-file: {self.copilot_output_file}")
        self.log(
            "  command: "
            f"copilot --yolo --model \"{self.copilot_model}\" --reasoning-effort \"{self.copilot_reasoning_effort}\" "
            f"--prompt \"<contents of {self.analysis_prompt_file}>\""
        )
        self.log("Prompt content begins")
        self.log(prompt.rstrip())
        self.log("Prompt content ends")

        command = [
            "copilot",
            "--yolo",
            "--model",
            self.copilot_model,
            "--reasoning-effort",
            self.copilot_reasoning_effort,
            "--prompt",
            prompt,
        ]
        child_env = os.environ.copy()
        child_env["COPILOT_GITHUB_TOKEN"] = copilot_token
        return_code = self.run_process_with_streaming(command, child_env)
        self.append_process_output("Copilot output")
        return return_code

    def persist_outputs(self) -> None:
        analysis_prompt = read_text(
            self.analysis_prompt_file,
            f"Analysis prompt file not found: {self.analysis_prompt_file}\n",
        )
        copilot_output = read_text(
            self.copilot_execution_log_file,
            f"Copilot output file not found: {self.copilot_execution_log_file}\n",
        )
        final_conclusion = self.final_conclusion or self.result_error_message

        write_text(self.copilot_output_artifact_file, copilot_output)
        write_text(self.final_conclusion_artifact_file, final_conclusion if final_conclusion.endswith("\n") else f"{final_conclusion}\n")

        with self.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"issue-number={self.issue_number}\n")
            handle.write(f"comment-id={self.comment_id}\n")
            handle.write(f"comment-url={self.comment_url}\n")
            handle.write(f"copilot-output-artifact-file={self.copilot_output_artifact_file}\n")
            handle.write(f"final-conclusion-artifact-file={self.final_conclusion_artifact_file}\n")
            handle.write(f"analysis-success={'true' if self.analysis_success else 'false'}\n")

        write_output("analysis-prompt", truncate_for_output(analysis_prompt, "analysis-prompt"), self.github_output)
        write_output("copilot-output", truncate_for_output(copilot_output, "copilot-output"), self.github_output)
        write_output("final-conclusion", truncate_for_output(final_conclusion, "final-conclusion"), self.github_output)
        write_output("failure-message", self.failure_message or "", self.github_output)

    def finalize(self) -> None:
        if self.copilot_answer_file.is_file():
            self.final_conclusion = self.copilot_answer_file.read_text(encoding="utf-8")
        else:
            self.final_conclusion = self.result_error_message

        final_comment = self.build_final_comment()
        write_text(self.final_comment_file, final_comment)

        if self.comment_id:
            try:
                self.update_comment(final_comment)
            except Exception as exc:
                self.log(f"Warning: failed to update final comment: {exc}")

        self.persist_outputs()

        print(read_text(self.copilot_execution_log_file).rstrip(), flush=True)
        print("\n---\n", flush=True)
        print(self.final_conclusion.rstrip(), flush=True)

    def run(self) -> None:
        self.prepare_files()
        try:
            self.issue_number = self.determine_issue_number()
            self.build_prompt()
            self.create_initial_comment()

            analysis_path = self.choose_analysis_path()
            return_code = self.run_litellm() if analysis_path == "litellm" else self.run_copilot()
            self.analysis_success = return_code == 0 and self.copilot_answer_file.is_file()
            if not self.analysis_success:
                self.failure_message = self.process_error_message
                self.log(self.process_error_message)
        except Exception as exc:
            self.analysis_success = False
            self.failure_message = str(exc)
            append_text(self.copilot_output_file, f"{self.failure_message}\n")
            self.log("Action execution failed:")
            self.log(traceback.format_exc().rstrip())
        finally:
            self.finalize()


def main() -> int:
    runner = ActionRunner()
    runner.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())