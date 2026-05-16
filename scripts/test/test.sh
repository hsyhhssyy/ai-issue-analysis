#!/usr/bin/env bash
# ===========================================================================
# Convenience launcher for AI Issue Analysis tests.
# Usage:
#   ./scripts/test/test.sh                   # Run local dry-run tests
#   ./scripts/test/test.sh --e2e             # Run E2E test on current repo
#   ./scripts/test/test.sh --e2e --repo owner/repo  # E2E on specific repo
#   ./scripts/test/test.sh --local --stage llm-config  # Run specific stage
#   ./scripts/test/test.sh --help            # Show full help
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

show_help() {
    cat <<'EOF'
AI Issue Analysis — Automated Test Suite
=========================================

Usage:
  ./scripts/test/test.sh [options]

Options (mode):
  --local                         Run local simulation tests (default)
  --e2e                           Run end-to-end GitHub integration tests

Options (local):
  --dry-run                       Skip LLM calls; only test wiring (default for --local)
  --full-run                      Actually invoke LLM (requires LLM_API_KEY)
  --stage <name>                  Run a specific stage: prepare, event-parsing,
                                  llm-config, mock-run

Options (E2E):
  --repo <owner/repo>             Target GitHub repository
  --title "<title>"               Test issue title
  --body "<body>"                 Test issue body text
  --body-file <path>              Read issue body from a file
  --workflow <filename.yml>       Workflow filename (default: ai-issue-analysis.yml)
  --ref <branch>                  Git ref to trigger workflow on (default: main)
  --bot-user <username>           GitHub username of the AI bot
  --no-cleanup                    Don't close the test issue after test
  --verbose                       Show detailed poll output

General:
  --help                          Show this help and exit

Examples:
  # Quick local wiring check
  ./scripts/test/test.sh

  # Full E2E test against current repo
  ./scripts/test/test.sh --e2e

  # E2E test against a specific repo with custom issue
  ./scripts/test/test.sh --e2e --repo my-org/my-repo \\
      --title "测试: 登录失败" \\
      --body-file ./test-data/login-issue.md

  # Run local config parser tests only
  ./scripts/test/test.sh --local --stage llm-config
EOF
}

# --- Parse arguments ---
MODE="local"
E2E_ARGS=()
LOCAL_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            show_help
            exit 0
            ;;
        --local)
            MODE="local"
            shift
            ;;
        --e2e)
            MODE="e2e"
            shift
            ;;
        --dry-run)
            LOCAL_ARGS+=(--dry-run)
            shift
            ;;
        --full-run)
            LOCAL_ARGS+=(--no-dry-run)
            shift
            ;;
        --stage)
            LOCAL_ARGS+=(--stage "$2")
            shift 2
            ;;
        --repo|--title|--workflow|--ref|--bot-user)
            E2E_ARGS+=("$1" "$2")
            shift 2
            ;;
        --body-file)
            E2E_ARGS+=("$1" "$2")
            shift 2
            ;;
        --body)
            E2E_ARGS+=("$1" "$2")
            shift 2
            ;;
        --no-cleanup)
            E2E_ARGS+=(--no-cleanup)
            shift
            ;;
        --verbose)
            E2E_ARGS+=(--verbose)
            shift
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

# --- Run ---
echo "=========================================="
echo " AI Issue Analysis — Test Suite"
echo " Mode: $MODE"
echo "=========================================="
echo ""

if [[ "$MODE" == "local" ]]; then
    # Default to dry-run if not specified
    if [[ " ${LOCAL_ARGS[*]} " != *"--dry-run"* ]] && [[ " ${LOCAL_ARGS[*]} " != *"--no-dry-run"* ]]; then
        LOCAL_ARGS+=(--dry-run)
    fi
    # Remove --no-dry-run -> pass no arg (full run)
    LOCAL_ARGS=("${LOCAL_ARGS[@]/--no-dry-run/}")
    exec python3 "$SCRIPT_DIR/test_local.py" "${LOCAL_ARGS[@]}"

elif [[ "$MODE" == "e2e" ]]; then
    # Check prerequisites
    if ! command -v gh &>/dev/null; then
        echo "[ERROR] GitHub CLI (gh) is not installed."
        echo "  Install it from: https://cli.github.com/"
        exit 1
    fi
    if ! gh auth status &>/dev/null; then
        echo "[ERROR] GitHub CLI is not authenticated."
        echo "  Run: gh auth login"
        exit 1
    fi
    exec python3 "$SCRIPT_DIR/test_e2e.py" "${E2E_ARGS[@]}"
fi
