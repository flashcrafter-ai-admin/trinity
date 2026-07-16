#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="$ROOT/scripts/deploy/github-actions-safe-deploy.sh"

# shellcheck source=../../scripts/deploy/github-actions-safe-deploy.sh
source "$SCRIPT"

expected=0123456789abcdef0123456789abcdef01234567
actual=$(parse_target_commit "deploy $expected")
[[ "$actual" == "$expected" ]]

for rejected in \
    "" \
    "deploy" \
    "deploy main" \
    "deploy $expected extra" \
    "bash -lc deploy $expected" \
    "deploy 0123456789ABCDEF0123456789ABCDEF01234567"; do
    if parse_target_commit "$rejected" >/dev/null 2>&1; then
        echo "unexpectedly accepted: $rejected" >&2
        exit 1
    fi
done

grep -q 'oauth-client-id:.*TS_OAUTH_CLIENT_ID' "$ROOT/.github/workflows/deploy-dev.yml"
grep -q 'tags:.*TS_TAGS' "$ROOT/.github/workflows/deploy-dev.yml"
grep -q 'DEV_PORT:.*DEV_PORT' "$ROOT/.github/workflows/deploy-dev.yml"
grep -q -- '-p "$DEV_PORT"' "$ROOT/.github/workflows/deploy-dev.yml"
grep -q 'StrictHostKeyChecking=yes' "$ROOT/.github/workflows/deploy-dev.yml"
grep -q 'deploy \$GITHUB_SHA' "$ROOT/.github/workflows/deploy-dev.yml"
! grep -q 'git stash' "$ROOT/.github/workflows/deploy-dev.yml"

echo "github-actions-safe-deploy: OK"
