#!/usr/bin/env bash
set -euo pipefail

# Usage:
# 1) cp .env.secrets.template .env.secrets.local
# 2) fill .env.secrets.local
# 3) ./scripts/upload_github_secrets.sh owner/repo
#
# Requirements:
# - gh CLI installed and authenticated (`gh auth login`)
# - repo argument OR current directory is a git repo with origin set

SECRETS_FILE=".env.secrets.local"
TARGET_REPO="${1:-}"

if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "Missing $SECRETS_FILE"
  echo "Create it from .env.secrets.template first."
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required. Install: https://cli.github.com/"
  exit 1
fi

if [[ -z "$TARGET_REPO" ]]; then
  # Try to derive from origin if available.
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
    if [[ "$ORIGIN_URL" =~ github\.com[:/]([^/]+/[^/.]+)(\.git)?$ ]]; then
      TARGET_REPO="${BASH_REMATCH[1]}"
    fi
  fi
fi

if [[ -z "$TARGET_REPO" ]]; then
  echo "Could not determine target repo."
  echo "Run: ./scripts/upload_github_secrets.sh owner/repo"
  exit 1
fi

echo "Uploading secrets to $TARGET_REPO ..."

while IFS= read -r line; do
  # Skip comments and empty lines
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  # Parse KEY=VALUE
  key="${line%%=*}"
  value="${line#*=}"

  # Skip malformed lines
  [[ -z "$key" || "$key" == "$value" ]] && continue

  # Skip empty values; user can leave optional ones blank.
  if [[ -z "$value" ]]; then
    continue
  fi

  # Trim possible surrounding whitespace in key
  key="$(echo "$key" | tr -d '[:space:]')"

  echo "  -> $key"
  gh secret set "$key" --repo "$TARGET_REPO" --body "$value"
done < "$SECRETS_FILE"

echo "Done. Secrets uploaded to $TARGET_REPO."

