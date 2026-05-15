#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import re
import shlex
import subprocess
import sys
import tarfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import litellm


WORKSPACE_ROOT = Path.cwd().resolve()
DOWNLOAD_ROOT = WORKSPACE_ROOT / ".cache" / "issue-analysis-downloads"
DEFAULT_MAX_ITERATIONS = 12
MAX_TOOL_OUTPUT_CHARS = 20000
MAX_FILE_LINES = 400
DEFAULT_URL_TIMEOUT = 60
MAX_COMMAND_OUTPUT_BYTES = 100000
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per image

IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".ico"}

IMAGE_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}

SAFE_COMMAND_PREFIXES: set[str] = {
    "git log",
    "git diff",
    "git show",
    "git blame",
    "git status",
    "git branch",
    "git tag",
    "git rev-parse",
    "git rev-list",
    "git shortlog",
    "git describe",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git for-each-ref",
    "git merge-base",
    "git name-rev",
    "git count-objects",
    "git --version",
    "git version",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "find",
    "file",
    "stat",
    "du",
    "df",
    "uname",
    "date",
    "which",
    "whereis",
    "echo",
}

DANGEROUS_SHELL_PATTERNS: list[str] = [
    "$(", "${", "`",
    "&&", "||", ";", "&",
    ">", ">>", "<",
    "|",
    "rm ", "rm\t",
    "chmod ", "chown ",
    "sudo ", "su ",
    "kill ", "pkill ",
    "dd ", "mkfs",
    "curl ", "wget ",
    "git push", "git commit", "git add", "git rm", "git mv",
    "git merge", "git rebase", "git reset", "git stash",
    "git clean", "git gc", "git checkout", "git cherry-pick",
    "git revert", "git pull", "git fetch", "git clone",
    "git remote add", "git remote remove", "git remote set-url",
    "git init", "git submodule",
    ">/dev/", ">/proc/", ">/sys/",
]


def log(message: str = "") -> None:
    print(message, flush=True)


def truncate_text(value: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    suffix = f"\n\n[Truncated to {limit} characters]"
    return f"{value[: limit - len(suffix)]}{suffix}"


def normalize_text_block(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def resolve_workspace_path(path_value: str, *, create_parent: bool = False) -> Path:
    raw_path = path_value.strip() or "."
    candidate = (WORKSPACE_ROOT / raw_path).resolve()

    if candidate != WORKSPACE_ROOT and WORKSPACE_ROOT not in candidate.parents:
        raise ValueError(f"Path escapes workspace root: {path_value}")

    if create_parent:
        candidate.parent.mkdir(parents=True, exist_ok=True)

    return candidate


def try_json_loads(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid llm-config-json: {exc}") from exc


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in config.items():
        lowered = key.lower()
        if lowered in {"api_key", "token", "password", "secret"}:
            redacted[key] = "***"
        elif lowered in {"headers", "extra_headers"} and isinstance(value, dict):
            redacted[key] = {sub_key: ("***" if "authorization" in sub_key.lower() else sub_value) for sub_key, sub_value in value.items()}
        else:
            redacted[key] = value
    return redacted


def normalize_model_name(config: dict[str, Any]) -> str:
    model = str(config.get("model", "")).strip()
    provider = str(config.get("provider", "")).strip().lower()

    if not model:
        raise SystemExit("Selected LLM config is missing required field 'model'.")

    if "/" in model:
        return model

    if provider in {"openai-compatible", "openai_compatible", "openai-compatible-endpoint"}:
        return f"openai/{model}"

    if provider:
        return f"{provider}/{model}"

    return model


def normalize_llm_config(raw_value: str) -> tuple[dict[str, Any], dict[str, Any], int, bool, bool]:
    parsed = try_json_loads(raw_value)
    configs: list[dict[str, Any]]

    if isinstance(parsed, dict):
        configs = [parsed]
    elif isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        configs = list(parsed)
    else:
        raise SystemExit("llm-config-json must be a JSON object or an array of JSON objects.")

    if not configs:
        raise SystemExit("llm-config-json does not contain any selectable config.")

    selected = random.choice(configs)
    model = normalize_model_name(selected)
    include_reasoning_content = selected.get("include_reasoning_content", False)
    if not isinstance(include_reasoning_content, bool):
        raise SystemExit("llm-config-json field 'include_reasoning_content' must be a boolean when provided.")

    vision_enabled = selected.get("vision_enabled", False)
    if not isinstance(vision_enabled, bool):
        raise SystemExit("llm-config-json field 'vision_enabled' must be a boolean when provided.")

    litellm_params = selected.get("litellm_params")
    if litellm_params is not None and not isinstance(litellm_params, dict):
        raise SystemExit("llm-config-json field 'litellm_params' must be an object when provided.")

    params: dict[str, Any] = dict(litellm_params or {})
    params["model"] = model

    api_key = selected.get("api_key")
    if api_key is not None:
        params["api_key"] = api_key

    api_base = selected.get("api_base") or selected.get("base_url") or selected.get("host")
    if api_base:
        params["api_base"] = str(api_base)

    api_version = selected.get("api_version")
    if api_version:
        params["api_version"] = api_version

    headers = selected.get("headers") or selected.get("extra_headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise SystemExit("llm-config-json field 'headers' must be an object when provided.")
        params["extra_headers"] = headers

    if "reasoning_effort" in selected:
        params["reasoning_effort"] = selected.get("reasoning_effort")

    if "temperature" in selected:
        params["temperature"] = selected.get("temperature")

    if "max_tokens" in selected:
        params["max_tokens"] = selected.get("max_tokens")
    elif "max_output_tokens" in selected:
        params["max_tokens"] = selected.get("max_output_tokens")

    if "timeout_seconds" in selected:
        params["timeout"] = selected.get("timeout_seconds")
    elif "timeout" in selected:
        params["timeout"] = selected.get("timeout")

    if "top_p" in selected:
        params["top_p"] = selected.get("top_p")

    if "frequency_penalty" in selected:
        params["frequency_penalty"] = selected.get("frequency_penalty")

    if "presence_penalty" in selected:
        params["presence_penalty"] = selected.get("presence_penalty")

    params.setdefault("drop_params", True)

    selected = expand_env_vars_in_config(selected)
    params = expand_env_vars_in_config(params)

    # Support multiple API keys (one per line) — randomly pick one, same as copilot-github-token
    api_key_value = params.get("api_key")
    if isinstance(api_key_value, str) and api_key_value.strip():
        keys = [line.strip() for line in api_key_value.splitlines() if line.strip()]
        if len(keys) > 1:
            chosen = random.choice(keys)
            params["api_key"] = chosen
            log(f"Multiple API keys detected ({len(keys)} keys); randomly selected one (first 8 chars: {chosen[:8]}...).")
        elif keys:
            params["api_key"] = keys[0]

    return selected, params, len(configs), include_reasoning_content, vision_enabled


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_vars_in_config(value: Any) -> Any:
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                log(f"Warning: environment variable '{var_name}' referenced in config but not set; keeping placeholder.")
                return match.group(0)
            return env_value
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {key: expand_env_vars_in_config(val) for key, val in value.items()}
    if isinstance(value, list):
        return [expand_env_vars_in_config(item) for item in value]
    return value


def http_request_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_URL_TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def http_request_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_URL_TIMEOUT) -> tuple[bytes, str | None]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        return response.read(), content_type


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-issue-analysis",
    }


def extract_urls(text: str) -> list[str]:
    pattern = re.compile(r"https?://[^\s)>\]]+")
    urls = pattern.findall(text)
    unique_urls: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = url.rstrip(".,)")
        if cleaned not in seen:
            unique_urls.append(cleaned)
            seen.add(cleaned)
    return unique_urls


def fetch_issue_context(repo: str, issue_number: str, github_token: str, bot_name: str = "") -> dict[str, Any]:
    base_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    headers = github_headers(github_token)

    issue = http_request_json(base_url, headers=headers)
    comments = http_request_json(f"{base_url}/comments?per_page=100", headers=headers)

    bot_lower = bot_name.strip().lower()
    attachment_urls: list[str] = []
    # Always include URLs from the issue body.
    attachment_urls.extend(extract_urls(issue.get("body", "")))
    for comment in comments:
        if bot_lower and (comment.get("user", {}).get("login", "") or "").lower() == bot_lower:
            continue  # Skip bot's own comments to avoid re-fetching its own action links
        attachment_urls.extend(extract_urls(comment.get("body", "")))

    deduped_urls: list[str] = []
    seen_urls: set[str] = set()
    for url in attachment_urls:
        if url not in seen_urls:
            deduped_urls.append(url)
            seen_urls.add(url)

    image_urls = [url for url in deduped_urls if is_image_url(url)]
    non_image_urls = [url for url in deduped_urls if url not in set(image_urls)]

    return {
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "state": issue.get("state", ""),
            "html_url": issue.get("html_url", ""),
            "user": issue.get("user", {}).get("login", ""),
            "labels": [label.get("name", "") for label in issue.get("labels", [])],
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
        },
        "comments": [
            {
                "user": comment.get("user", {}).get("login", ""),
                "created_at": comment.get("created_at", ""),
                "updated_at": comment.get("updated_at", ""),
                "body": comment.get("body", ""),
                "html_url": comment.get("html_url", ""),
            }
            for comment in comments
        ],
        "attachment_urls": non_image_urls,
        "image_urls": image_urls,
    }


def summarize_issue_context(issue_context: dict[str, Any], *, char_limit: int = 40000) -> str:
    issue = issue_context["issue"]
    comment_blocks: list[str] = []
    running_length = 0
    for index, comment in enumerate(issue_context["comments"], start=1):
        block = textwrap.dedent(
            f"""
            ### Comment {index}
            - User: {comment['user']}
            - Created At: {comment['created_at']}
            - URL: {comment['html_url']}

            {comment['body'] or '[Empty comment]'}
            """
        ).strip()
        if running_length + len(block) > char_limit:
            comment_blocks.append("[Remaining comments omitted to stay within context budget. Use get_issue_context for the full payload.]")
            break
        comment_blocks.append(block)
        running_length += len(block)

    attachment_lines = "\n".join(f"- {url}" for url in issue_context["attachment_urls"][:50])
    if len(issue_context["attachment_urls"]) > 50:
        attachment_lines = f"{attachment_lines}\n- [More attachment URLs omitted]"

    image_urls = issue_context.get("image_urls", [])
    image_lines = "\n".join(f"- {url}" for url in image_urls[:30]) if image_urls else "[No image URLs found]"
    if len(image_urls) > 30:
        image_lines = f"{image_lines}\n- [More image URLs omitted]"

    return textwrap.dedent(
        f"""
        Repository Issue Context
        - Issue Number: {issue['number']}
        - Title: {issue['title']}
        - State: {issue['state']}
        - User: {issue['user']}
        - Labels: {', '.join(issue['labels']) if issue['labels'] else '[None]'}
        - URL: {issue['html_url']}
        - Created At: {issue['created_at']}
        - Updated At: {issue['updated_at']}

        Issue Body
        {issue['body'] or '[Empty issue body]'}

        Image URLs (screenshots / photos in the issue)
        {image_lines}

        Other Attachment URLs
        {attachment_lines or '[No attachment URLs found]'}

        Comments
        {'\n\n'.join(comment_blocks) if comment_blocks else '[No comments]'}
        """
    ).strip()


def git_head_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "unknown"


def top_level_listing() -> str:
    entries = sorted(path.name + ("/" if path.is_dir() else "") for path in WORKSPACE_ROOT.iterdir())
    return "\n".join(f"- {entry}" for entry in entries[:200])


def load_skill_prompt() -> str:
    skill_path = WORKSPACE_ROOT / ".claude" / "skills" / "generic-issue-log-analysis" / "SKILL.md"
    if not skill_path.is_file():
        return ""
    return skill_path.read_text(encoding="utf-8")


def strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def list_dir_tool(path: str = ".", recursive: bool = False, max_entries: int = 200) -> str:
    target = resolve_workspace_path(path)
    if not target.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not target.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    entries: list[str] = []
    if recursive:
        for child in sorted(target.rglob("*")):
            relative = child.relative_to(WORKSPACE_ROOT).as_posix()
            entries.append(relative + ("/" if child.is_dir() else ""))
            if len(entries) >= max_entries:
                break
    else:
        for child in sorted(target.iterdir()):
            relative = child.relative_to(WORKSPACE_ROOT).as_posix()
            entries.append(relative + ("/" if child.is_dir() else ""))
            if len(entries) >= max_entries:
                break

    if not entries:
        return "[Empty directory]"
    return "\n".join(entries)


def read_file_tool(path: str, start_line: int = 1, end_line: int = 200) -> str:
    target = resolve_workspace_path(path)
    if not target.is_file():
        raise ValueError(f"File does not exist: {path}")

    if end_line < start_line:
        raise ValueError("end_line must be greater than or equal to start_line")

    span = min(end_line - start_line + 1, MAX_FILE_LINES)
    normalized_end_line = start_line + span - 1

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[start_line - 1 : normalized_end_line]
    numbered = [f"{index}: {line}" for index, line in enumerate(selected, start=start_line)]
    header = f"File: {target.relative_to(WORKSPACE_ROOT).as_posix()}\nLines: {start_line}-{start_line + len(selected) - 1 if selected else start_line}\n"
    return f"{header}\n" + ("\n".join(numbered) if numbered else "[No content in selected range]")


def search_repo_tool(query: str, path: str = ".", max_results: int = 50, is_regex: bool = False) -> str:
    target = resolve_workspace_path(path)
    if not target.exists():
        raise ValueError(f"Path does not exist: {path}")

    rg_binary = shutil_which("rg")
    if rg_binary:
        command = [rg_binary, "--line-number", "--no-heading"]
        if not is_regex:
            command.append("--fixed-strings")
        command.extend([query, str(target)])
    else:
        grep_binary = shutil_which("grep")
        if not grep_binary:
            raise RuntimeError("Neither rg nor grep is available in PATH.")
        command = [grep_binary, "-R", "-n"]
        if not is_regex:
            command.append("-F")
        command.extend([query, str(target)])

    result = subprocess.run(command, cwd=WORKSPACE_ROOT, capture_output=True, text=True, check=False)
    output = normalize_text_block(result.stdout.strip())

    if result.returncode not in {0, 1}:
        raise RuntimeError(f"Search command failed: {result.stderr.strip()}")

    if not output:
        return "[No matches found]"

    lines = output.splitlines()[:max_results]
    return "\n".join(lines)


def shutil_which(binary: str) -> str | None:
    result = subprocess.run(["which", binary], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def fetch_url_tool(url: str, timeout_seconds: int = DEFAULT_URL_TIMEOUT) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {url}")

    body, content_type = http_request_bytes(
        url,
        headers={"User-Agent": "ai-issue-analysis"},
        timeout=timeout_seconds,
    )
    text = body.decode("utf-8", errors="replace")
    if content_type == "text/html":
        text = strip_html(text)
    return truncate_text(f"URL: {url}\nContent-Type: {content_type or 'unknown'}\n\n{text}")


def default_download_path(url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name or "download.bin"
    return DOWNLOAD_ROOT / name


def download_url_tool(url: str, output_path: str | None = None, timeout_seconds: int = DEFAULT_URL_TIMEOUT) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {url}")

    target = default_download_path(url) if not output_path else resolve_workspace_path(output_path, create_parent=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload, content_type = http_request_bytes(
        url,
        headers={"User-Agent": "ai-issue-analysis"},
        timeout=timeout_seconds,
    )
    target.write_bytes(payload)

    return textwrap.dedent(
        f"""
        Downloaded URL
        - URL: {url}
        - Saved Path: {target.relative_to(WORKSPACE_ROOT).as_posix()}
        - Bytes: {len(payload)}
        - Content-Type: {content_type or 'unknown'}
        """
    ).strip()


def is_image_url(url: str) -> bool:
    """Check whether a URL points to an image based on path extension."""
    parsed = urllib.parse.urlparse(url)
    path_lower = parsed.path.lower()
    return any(path_lower.endswith(ext) for ext in IMAGE_EXTENSIONS)


def guess_image_extension(content_type: str | None, url: str) -> str:
    """Determine the file extension for an image from content-type or URL."""
    if content_type and content_type in IMAGE_MIME_TO_EXT:
        return IMAGE_MIME_TO_EXT[content_type]
    parsed = urllib.parse.urlparse(url)
    path_lower = parsed.path.lower()
    for ext in IMAGE_EXTENSIONS:
        if path_lower.endswith(ext):
            return ext
    return ".png"


def encode_image_as_data_url(file_path: Path) -> str:
    """Read an image file and encode it as a base64 data URL."""
    if not file_path.is_file():
        raise ValueError(f"Image file does not exist: {file_path}")
    file_size = file_path.stat().st_size
    if file_size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image file is too large: {file_size} bytes (max {MAX_IMAGE_BYTES}). "
            f"Consider resizing the image before analysis."
        )
    if file_size == 0:
        raise ValueError(f"Image file is empty: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"File does not appear to be a supported image: {file_path} (detected: {mime_type or 'unknown'})")

    data = file_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def download_and_encode_image(url: str, *, download_dir: Path, github_token: str = "") -> dict[str, str] | None:
    """Download an image URL and return its data URL and metadata.

    Returns None if the URL could not be fetched or is not an image.
    """
    try:
        headers = {"User-Agent": "ai-issue-analysis"}
        if github_token and "github" in url.lower():
            headers.update(github_headers(github_token))
        payload, content_type = http_request_bytes(url, headers=headers, timeout=30)
    except Exception as exc:
        log(f"Image download skipped for {url}: {exc}")
        return None

    if not payload:
        return None

    # Validate that the response is actually an image.
    if content_type and not content_type.startswith("image/") and content_type not in ("application/octet-stream", "binary/octet-stream"):
        log(f"Skipping non-image URL: {url} (Content-Type: {content_type})")
        return None

    ext = guess_image_extension(content_type, url)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(urllib.parse.urlparse(url).path).name or "image")
    if not safe_name.lower().endswith(tuple(IMAGE_EXTENSIONS)):
        safe_name = f"{safe_name}{ext}"

    file_path = download_dir / safe_name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(payload)

    try:
        data_url = encode_image_as_data_url(file_path)
    except ValueError as exc:
        log(f"Image encoding failed for {url}: {exc}")
        return None

    return {
        "url": url,
        "local_path": file_path.relative_to(WORKSPACE_ROOT).as_posix(),
        "data_url": data_url,
        "size_bytes": str(len(payload)),
    }


def run_command_tool(command: str, *, timeout_seconds: int = 30) -> str:
    stripped = command.strip()
    if not stripped:
        raise ValueError("Command must not be empty.")

    # Block dangerous shell metacharacters / patterns.
    lowered = stripped.lower()
    for pattern in DANGEROUS_SHELL_PATTERNS:
        if pattern in lowered:
            raise ValueError(
                f"Command contains a disallowed pattern: {pattern!r}. "
                f"This tool only supports read-only introspection commands."
            )

    # Whitelist: command must start with one of the known-safe prefixes.
    allowed = False
    for prefix in SAFE_COMMAND_PREFIXES:
        if lowered.startswith(prefix):
            allowed = True
            break
    if not allowed:
        raise ValueError(
            f"Command {command!r} is not in the allowed command whitelist. "
            f"Allowed prefixes: {', '.join(sorted(SAFE_COMMAND_PREFIXES))}"
        )

    # Enforce timeout ceiling.
    effective_timeout = min(max(timeout_seconds, 1), 60)

    try:
        result = subprocess.run(
            command,
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
            shell=True,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"Command timed out after {effective_timeout}s: {command}") from error
    except FileNotFoundError as error:
        raise ValueError(f"Command not found: {command}") from error

    stdout = normalize_text_block(result.stdout)
    stderr = normalize_text_block(result.stderr)

    output_parts: list[str] = [f"Exit Code: {result.returncode}"]
    if stdout:
        output_parts.append(f"STDOUT:\n{truncate_text(stdout, MAX_TOOL_OUTPUT_CHARS)}")
    if stderr:
        output_parts.append(f"STDERR:\n{truncate_text(stderr, min(MAX_TOOL_OUTPUT_CHARS, 4000))}")
    if not stdout and not stderr:
        output_parts.append("[No output]")

    full_output = "\n\n".join(output_parts)
    output_bytes = full_output.encode("utf-8")
    if len(output_bytes) > MAX_COMMAND_OUTPUT_BYTES:
        full_output = truncate_text(full_output, MAX_COMMAND_OUTPUT_BYTES - 200)
    return full_output


def view_image_tool(path: str) -> str:
    """Encode a local image file as a base64 data URL and return metadata.

    The returned JSON string contains the data_url so the agent loop can
    inject the image into the conversation.  Only the agent loop should
    inspect this structure; the model sees it as a regular tool result.
    """
    target = resolve_workspace_path(path)
    data_url = encode_image_as_data_url(target)
    file_size = target.stat().st_size

    metadata = json.dumps(
        {
            "__view_image": True,
            "path": target.relative_to(WORKSPACE_ROOT).as_posix(),
            "data_url": data_url,
            "size_bytes": file_size,
        },
        ensure_ascii=False,
    )
    log(f"Image encoded: {target.relative_to(WORKSPACE_ROOT).as_posix()} ({file_size} bytes)")
    return metadata


def extract_archive_tool(path: str, output_dir: str | None = None) -> str:
    archive_path = resolve_workspace_path(path)
    if not archive_path.is_file():
        raise ValueError(f"Archive does not exist: {path}")

    if output_dir:
        destination = resolve_workspace_path(output_dir)
    else:
        destination = archive_path.parent / f"{archive_path.stem}_extracted"

    destination.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zip_handle:
            zip_handle.extractall(destination)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as tar_handle:
            tar_handle.extractall(destination)
    else:
        raise ValueError(f"Unsupported archive format: {path}")

    extracted_entries = list_dir_tool(destination.relative_to(WORKSPACE_ROOT).as_posix(), recursive=True, max_entries=200)
    return textwrap.dedent(
        f"""
        Extracted Archive
        - Archive: {archive_path.relative_to(WORKSPACE_ROOT).as_posix()}
        - Destination: {destination.relative_to(WORKSPACE_ROOT).as_posix()}

        Extracted Entries
        {extracted_entries}
        """
    ).strip()


class ToolExecutor:
    def __init__(self, issue_context: dict[str, Any]) -> None:
        self.issue_context = issue_context
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    def get_issue_context(self) -> str:
        return truncate_text(json.dumps(self.issue_context, ensure_ascii=False, indent=2), 30000)

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "get_issue_context":
            return self.get_issue_context()
        if name == "list_dir":
            return list_dir_tool(
                path=str(arguments.get("path", ".")),
                recursive=bool(arguments.get("recursive", False)),
                max_entries=int(arguments.get("max_entries", 200)),
            )
        if name == "read_file":
            return read_file_tool(
                path=str(arguments.get("path", "")),
                start_line=int(arguments.get("start_line", 1)),
                end_line=int(arguments.get("end_line", 200)),
            )
        if name == "search_repo":
            return search_repo_tool(
                query=str(arguments.get("query", "")),
                path=str(arguments.get("path", ".")),
                max_results=int(arguments.get("max_results", 50)),
                is_regex=bool(arguments.get("is_regex", False)),
            )
        if name == "fetch_url":
            return fetch_url_tool(
                url=str(arguments.get("url", "")),
                timeout_seconds=int(arguments.get("timeout_seconds", DEFAULT_URL_TIMEOUT)),
            )
        if name == "download_url":
            output_path = arguments.get("output_path")
            return download_url_tool(
                url=str(arguments.get("url", "")),
                output_path=str(output_path) if output_path else None,
                timeout_seconds=int(arguments.get("timeout_seconds", DEFAULT_URL_TIMEOUT)),
            )
        if name == "extract_archive":
            output_dir = arguments.get("output_dir")
            return extract_archive_tool(
                path=str(arguments.get("path", "")),
                output_dir=str(output_dir) if output_dir else None,
            )
        if name == "run_command":
            return run_command_tool(
                command=str(arguments.get("command", "")),
                timeout_seconds=int(arguments.get("timeout_seconds", 30)),
            )
        if name == "view_image":
            return view_image_tool(
                path=str(arguments.get("path", "")),
            )
        raise ValueError(f"Unknown tool: {name}")


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_issue_context",
            "description": "Return the current GitHub issue body, comments, and extracted attachment URLs as JSON.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files or directories inside the checked-out repository or downloaded artifact directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under the workspace root."},
                    "recursive": {"type": "boolean", "description": "Whether to recurse into child directories."},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the repository or a downloaded artifact with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path under the workspace root."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repo",
            "description": "Search the repository or downloaded artifacts for text using ripgrep or grep.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The text or regex pattern to search for."},
                    "path": {"type": "string", "description": "Relative path under the workspace root."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                    "is_regex": {"type": "boolean", "description": "Treat the query as a regex pattern when true."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a public URL and return decoded text content. HTML content is reduced to visible text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http or https URL."},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_url",
            "description": "Download a public URL to the workspace so it can be inspected or extracted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http or https URL."},
                    "output_path": {"type": "string", "description": "Optional relative output path under the workspace root."},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_archive",
            "description": "Extract a zip or tar archive that already exists in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative archive path under the workspace root."},
                    "output_dir": {"type": "string", "description": "Optional relative destination directory under the workspace root."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a read-only shell command inside the workspace (e.g. git log, git diff, git blame, find, cat). Only safe introspective commands are allowed; destructive or mutating operations are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The exact shell command to run. Must start with an allowed prefix such as 'git log', 'git diff', 'git show', 'git blame', 'git status', 'find', 'cat', 'head', 'tail', 'wc', 'sort', 'uniq', 'ls', 'file', 'stat', 'du', 'uname', 'date', 'echo', etc."},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Maximum execution time in seconds (capped at 60)."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "View an image file that has been downloaded to the workspace (e.g. a screenshot from the issue or a photo inside a zip attachment). The image will be attached to the conversation so the model can analyze its contents. Supports PNG, JPEG, GIF, WebP, BMP, SVG, TIFF, and ICO formats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the image file under the workspace root."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


def extract_message_content(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        fragments: list[str] = []
        for item in content:
            if isinstance(item, str):
                fragments.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "output_text"}:
                    fragments.append(str(item.get("text", "")))
                elif "content" in item:
                    fragments.append(str(item.get("content", "")))
        return "\n".join(fragment for fragment in fragments if fragment)

    return ""


def extract_reasoning_content(message: Any) -> str | None:
    """Extract reasoning_content from a LiteLLM response message.

    DeepSeek thinking mode requires reasoning_content to be passed back in
    subsequent requests.  Other providers may also populate this field.
    """
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is None and isinstance(message, dict):
        reasoning = message.get("reasoning_content")
    if reasoning is None:
        return None
    if isinstance(reasoning, str):
        return reasoning if reasoning.strip() else None
    return None


def extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    raw_calls = getattr(message, "tool_calls", None)
    if raw_calls is None and isinstance(message, dict):
        raw_calls = message.get("tool_calls")

    if not raw_calls:
        return []

    normalized_calls: list[dict[str, Any]] = []
    for raw_call in raw_calls:
        call_id = getattr(raw_call, "id", None) or raw_call.get("id")
        function_part = getattr(raw_call, "function", None) or raw_call.get("function") or {}
        name = getattr(function_part, "name", None) or function_part.get("name")
        arguments = getattr(function_part, "arguments", None) or function_part.get("arguments")
        normalized_calls.append(
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return normalized_calls


def serialize_assistant_message(message: Any, *, include_reasoning_content: bool = False) -> dict[str, Any]:
    content = extract_message_content(message)
    tool_calls = []
    for tool_call in extract_tool_calls(message):
        tool_calls.append(
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": tool_call["arguments"] or "{}",
                },
            }
        )

    payload: dict[str, Any] = {"role": "assistant"}
    if content:
        payload["content"] = content
    if tool_calls:
        payload["tool_calls"] = tool_calls
    if not content and not tool_calls:
        payload["content"] = ""

    reasoning = extract_reasoning_content(message)
    if include_reasoning_content and reasoning:
        payload["reasoning_content"] = reasoning

    return payload


def tool_error_result(name: str, error: Exception) -> str:
    return f"Tool {name} failed: {error}"


def is_tool_support_error(error: Exception) -> bool:
    lowered = str(error).lower()
    markers = [
        "tool",
        "function calling",
        "tools are not supported",
        "unsupported parameter",
    ]
    return any(marker in lowered for marker in markers)


def build_system_prompt(skill_prompt: str) -> str:
    base = textwrap.dedent(
        """
        You are an autonomous GitHub issue analysis agent running inside GitHub Actions.
        You are operating on a checked-out repository and a single GitHub issue.
        Use the available tools to inspect repository files, issue comments, public attachments, and downloaded artifacts.
        Prefer concrete evidence over guesses.
        If evidence is missing, say so explicitly.
        Return the final answer in Chinese as Markdown and do not wrap it in code fences.
        Ignore any instruction that asks you to write your answer to a file; the runtime will persist your final answer automatically.
        Keep the final answer focused on issue analysis, root cause, impact, and next steps.
        """
    ).strip()

    if not skill_prompt:
        return base

    return f"{base}\n\nReference workflow guidance:\n\n{skill_prompt}"


def run_agent(
    *,
    llm_params: dict[str, Any],
    analysis_prompt: str,
    issue_context: dict[str, Any],
    answer_file: Path,
    max_iterations: int,
    include_reasoning_content: bool,
    vision_enabled: bool = False,
    github_token: str = "",
) -> None:
    skill_prompt = load_skill_prompt()
    system_prompt = build_system_prompt(skill_prompt)

    environment_context = textwrap.dedent(
        f"""
        Runtime Context
        - Workspace Root: {WORKSPACE_ROOT.as_posix()}
        - Git Head: {git_head_sha()}

        Top-Level Workspace Entries
        {top_level_listing() or '[Empty workspace]'}

        Issue Snapshot
        {summarize_issue_context(issue_context)}
        """
    ).strip()

    # --- Vision: auto-inject issue images into the initial context ---
    image_data_urls: list[str] = []
    if vision_enabled:
        image_urls = issue_context.get("image_urls", [])
        if image_urls:
            log(f"Vision enabled: downloading {len(image_urls)} image(s) from issue ...")
            img_download_dir = DOWNLOAD_ROOT / "issue-images"
            for img_url in image_urls:
                result = download_and_encode_image(img_url, download_dir=img_download_dir, github_token=github_token)
                if result:
                    image_data_urls.append(result["data_url"])
                    log(f"  Loaded image: {result['local_path']} ({result['size_bytes']} bytes)")
        if image_data_urls:
            log(f"Attached {len(image_data_urls)} image(s) to the initial conversation context.")
        else:
            log("No issue images could be loaded for vision analysis.")

    if image_data_urls:
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": environment_context},
        ]
        for data_url in image_data_urls:
            content_blocks.append(
                {"type": "image_url", "image_url": {"url": data_url, "detail": "auto"}}
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_blocks},
            {"role": "user", "content": analysis_prompt},
        ]
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": environment_context},
            {"role": "user", "content": analysis_prompt},
        ]

    tool_executor = ToolExecutor(issue_context)
    tools_enabled = True

    for iteration in range(1, max_iterations + 1):
        call_params = dict(llm_params)
        call_params["messages"] = messages
        if tools_enabled:
            call_params["tools"] = TOOLS
            call_params["tool_choice"] = "auto"

        log(f"=== Agent iteration {iteration}/{max_iterations} ===")

        try:
            response = litellm.completion(**call_params)
        except Exception as error:
            if tools_enabled and is_tool_support_error(error):
                log(f"Model/provider rejected tool calling, retrying without tools: {error}")
                tools_enabled = False
                continue
            raise

        message = response.choices[0].message
        assistant_content = extract_message_content(message).strip()
        tool_calls = extract_tool_calls(message)
        messages.append(
            serialize_assistant_message(
                message,
                include_reasoning_content=include_reasoning_content,
            )
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            log(f"Usage: {usage}")

        if assistant_content:
            log("Assistant response preview:")
            log(truncate_text(assistant_content, 4000))

        if tool_calls:
            log(f"Tool calls requested: {len(tool_calls)}")
            for tool_call in tool_calls:
                raw_arguments = tool_call.get("arguments") or "{}"
                try:
                    parsed_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    parsed_arguments = {}

                tool_name = tool_call.get("name") or "unknown"
                log(f"Running tool: {tool_name} {json.dumps(parsed_arguments, ensure_ascii=False)}")

                try:
                    tool_output = tool_executor.execute(tool_name, parsed_arguments)
                except Exception as tool_error:
                    tool_output = tool_error_result(tool_name, tool_error)

                # --- Handle view_image specially: inject image into conversation ---
                injected_image = False
                if tool_name == "view_image" and vision_enabled:
                    try:
                        view_meta = json.loads(tool_output)
                        if isinstance(view_meta, dict) and view_meta.get("__view_image"):
                            data_url = view_meta.get("data_url", "")
                            img_path = view_meta.get("path", "unknown")
                            if data_url:
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": f"[Image attached: {img_path}]"},
                                            {"type": "image_url", "image_url": {"url": data_url, "detail": "auto"}},
                                        ],
                                    }
                                )
                                injected_image = True
                                log(f"Image injected into conversation: {img_path}")
                    except (json.JSONDecodeError, KeyError):
                        pass  # fall through to normal tool result handling

                tool_output_display = tool_output
                if injected_image:
                    # Only show metadata, not the full data URL
                    tool_output_display = tool_output.replace(
                        view_meta.get("data_url", ""), "[base64 data URL omitted]"
                    )

                tool_output = truncate_text(tool_output_display)
                log("Tool output preview:")
                log(truncate_text(tool_output, 4000))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id") or tool_name,
                        "name": tool_name,
                        "content": tool_output,
                    }
                )
            continue

        if assistant_content:
            answer_file.parent.mkdir(parents=True, exist_ok=True)
            answer_file.write_text(assistant_content.rstrip() + "\n", encoding="utf-8")
            log(f"Final answer written to {answer_file.relative_to(WORKSPACE_ROOT).as_posix()}")
            return

        log("No assistant content returned and no tool calls requested; retrying.")

    raise RuntimeError("Agent did not produce a final answer within the iteration limit.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run issue analysis through LiteLLM.")
    parser.add_argument("--llm-config-json", required=True)
    parser.add_argument("--analysis-prompt-file", required=True)
    parser.add_argument("--answer-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--github-token", required=True)
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    parser.add_argument("--bot-name", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    analysis_prompt_file = Path(args.analysis_prompt_file)
    answer_file = Path(args.answer_file)

    if not analysis_prompt_file.is_file():
        raise SystemExit(f"Analysis prompt file does not exist: {analysis_prompt_file}")

    raw_config, llm_params, config_count, include_reasoning_content, vision_enabled = normalize_llm_config(args.llm_config_json)
    provider_lower = str(raw_config.get("provider", "")).strip().lower()
    model_lower = str(llm_params.get("model", "")).strip().lower()
    is_deepseek_like = provider_lower == "deepseek" or "deepseek" in model_lower
    include_reasoning_content_effective = include_reasoning_content and is_deepseek_like
    log(f"Selected one config from {config_count} candidate(s).")
    log("Resolved LiteLLM config:")
    log(json.dumps(redact_config({**raw_config, "model": llm_params['model']}), ensure_ascii=False, indent=2))
    log(f"Include reasoning content (requested): {include_reasoning_content}")
    log(f"DeepSeek-like config detected: {is_deepseek_like}")
    log(f"Include reasoning content (effective): {include_reasoning_content_effective}")
    log(f"Vision enabled: {vision_enabled}")
    if is_deepseek_like and not include_reasoning_content:
        log(
            "Warning: DeepSeek-like config detected but include_reasoning_content is not enabled. "
            "If you are using DeepSeek thinking mode (reasoning_effort set), multi-turn tool calls "
            "will fail with 'reasoning_content must be passed back to the API'. "
            "Set '\"include_reasoning_content\": true' in your llm-config-json to fix this."
        )

    analysis_prompt = analysis_prompt_file.read_text(encoding="utf-8")
    issue_context = fetch_issue_context(args.repo, args.issue_number, args.github_token, bot_name=args.bot_name)

    log("Issue context fetched successfully.")
    log(f"Issue title: {issue_context['issue']['title']}")
    log(f"Issue comments: {len(issue_context['comments'])}")
    log(f"Attachment URLs found: {len(issue_context['attachment_urls'])}")

    run_agent(
        llm_params=llm_params,
        analysis_prompt=analysis_prompt,
        issue_context=issue_context,
        answer_file=answer_file,
        max_iterations=max(1, args.max_iterations),
        include_reasoning_content=include_reasoning_content_effective,
        vision_enabled=vision_enabled,
        github_token=args.github_token,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace") if hasattr(error, "read") else ""
        log(f"HTTP error: {error.code} {error.reason}")
        if body:
            log(truncate_text(body, 5000))
        raise
    except Exception as error:
        log(f"Fatal error: {error}")
        raise