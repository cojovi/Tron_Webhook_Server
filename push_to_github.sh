#!/usr/bin/env bash
# Run this AFTER: gh auth login
# Creates github.com/<your-username>/Tron_Webhook_Server and pushes main.

set -euo pipefail
cd "$(dirname "$0")"
export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Install GitHub CLI: https://cli.github.com/ (or ensure gh is on PATH)"
  exit 1
fi

gh auth status

echo "Creating repo Tron_Webhook_Server and pushing..."
gh repo create Tron_Webhook_Server \
  --public \
  --source=. \
  --remote=origin \
  --description "Universal webhook collector with SQLite storage and Aurora-themed Textual TUI" \
  --push

echo "Done. Remote:"
git remote -v
