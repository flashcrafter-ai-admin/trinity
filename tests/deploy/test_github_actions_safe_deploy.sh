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
grep -q 'TRINITY_EXPECTED_SOURCE_REVISION="$target_commit"' "$SCRIPT"
[[ "$(grep -c 'assert_governed_source' "$ROOT/scripts/deploy/safe-upgrade.sh")" -ge 5 ]]
grep -q '.git_commit == $commit' "$ROOT/scripts/deploy/safe-upgrade.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q
git -C "$tmp" config user.email test@example.com
git -C "$tmp" config user.name test
printf 'governed\n' > "$tmp/tracked.txt"
printf 'ignored.txt\n' > "$tmp/.gitignore"
git -C "$tmp" add .gitignore tracked.txt
git -C "$tmp" commit -qm governed
commit=$(git -C "$tmp" rev-parse HEAD)
assert_exact_clean_worktree "$tmp" "$commit"

printf 'dirty\n' >> "$tmp/tracked.txt"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "tracked source drift was accepted" >&2
    exit 1
fi
git -C "$tmp" restore tracked.txt

printf 'untracked\n' > "$tmp/untracked.txt"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "untracked source drift was accepted" >&2
    exit 1
fi
rm "$tmp/untracked.txt"
assert_exact_clean_worktree "$tmp" "$commit"

printf 'ignored\n' > "$tmp/ignored.txt"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "ignored source drift was accepted" >&2
    exit 1
fi
rm "$tmp/ignored.txt"

git -C "$tmp" update-index --assume-unchanged tracked.txt
printf 'hidden\n' >> "$tmp/tracked.txt"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "assume-unchanged source drift was accepted" >&2
    exit 1
fi
git -C "$tmp" update-index --no-assume-unchanged tracked.txt
git -C "$tmp" restore tracked.txt
assert_exact_clean_worktree "$tmp" "$commit"

git -C "$tmp" update-index --skip-worktree tracked.txt
printf 'hidden\n' >> "$tmp/tracked.txt"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "skip-worktree source drift was accepted" >&2
    exit 1
fi
git -C "$tmp" update-index --no-skip-worktree tracked.txt
git -C "$tmp" restore tracked.txt
assert_exact_clean_worktree "$tmp" "$commit"

replacement=$(printf 'replacement commit\n' \
    | git -C "$tmp" -c user.name=test -c user.email=test@example.com \
        commit-tree "${commit}^{tree}")
git -C "$tmp" replace "$commit" "$replacement"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "repository replacement ref was accepted" >&2
    exit 1
fi

echo "github-actions-safe-deploy: OK"
