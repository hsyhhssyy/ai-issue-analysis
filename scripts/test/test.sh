#!/usr/bin/env bash
# ===========================================================================
# Convenience launcher for AI Issue Analysis E2E test.
# Usage:
#   ./scripts/test/test.sh                   # Run on current repo
#   ./scripts/test/test.sh --repo owner/repo # Run on specific repo
#   ./scripts/test/test.sh --body-file .temp/test-issue.md  # Custom body
#   ./scripts/test/test.sh --no-cleanup      # Keep test issue open
#   ./scripts/test/test.sh --help            # Show full help
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

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

echo "=========================================="
echo " AI Issue Analysis — E2E Test"
echo "=========================================="
echo ""

exec python3 "$SCRIPT_DIR/test_e2e.py" "$@"
