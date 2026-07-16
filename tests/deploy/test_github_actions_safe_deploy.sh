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
grep -q '/usr/bin/env -i' "$SCRIPT"
grep -q 'GIT_CONFIG_GLOBAL=/dev/null' "$SCRIPT"
grep -q 'GIT_ALLOW_PROTOCOL=https' "$SCRIPT"
grep -q 'GIT_TERMINAL_PROMPT=0' "$SCRIPT"
grep -q 'GIT_CONFIG_VALUE_10=' "$SCRIPT"
grep -q "GIT_CONFIG_VALUE_11='!gh auth git-credential'" "$SCRIPT"
grep -q -- '--no-ext-diff --no-textconv --quiet' "$SCRIPT"

tmp=$(mktemp -d)
configured_worktree=$(mktemp -d)
trap 'rm -rf "$tmp" "$configured_worktree"' EXIT
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

cp "$tmp/.gitignore" "$configured_worktree/.gitignore"
cp "$tmp/tracked.txt" "$configured_worktree/tracked.txt"
git -C "$tmp" config core.worktree "$configured_worktree"
printf 'hidden by local core.worktree\n' >> "$tmp/tracked.txt"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "repository-local worktree redirection was accepted" >&2
    exit 1
fi
git -C "$tmp" config --unset core.worktree
git -C "$tmp" restore tracked.txt
assert_exact_clean_worktree "$tmp" "$commit"

credential_helper_marker="$configured_worktree/local-credential-helper-ran"
askpass_marker="$configured_worktree/caller-askpass-ran"
askpass_helper="$configured_worktree/caller-askpass"
printf '#!/bin/sh\nprintf local > %q\nprintf probe-value\\n\n' \
    "$askpass_marker" > "$askpass_helper"
chmod 700 "$askpass_helper"
git -C "$tmp" config credential.helper \
    "!f() { printf local > '$credential_helper_marker'; return 1; }; f"
git -C "$tmp" config core.askPass "$askpass_helper"
set +e
printf 'protocol=https\nhost=github.com\n\n' \
    | HOME="$configured_worktree" \
        GH_CONFIG_DIR="$configured_worktree" \
        GH_TOKEN=caller-controlled-token \
        GITHUB_TOKEN=caller-controlled-token \
        GIT_ASKPASS="$askpass_helper" \
        SSH_ASKPASS="$askpass_helper" \
        GIT_SSH_COMMAND="$askpass_helper" \
        git_no_replace -C "$tmp" credential fill >/dev/null 2>&1
credential_fill_status=$?
set -e
if [[ -e "$credential_helper_marker" ]]; then
    echo "repository-local credential helper executed" >&2
    exit 1
fi
if [[ -e "$askpass_marker" ]]; then
    echo "caller or repository-local askpass executed" >&2
    exit 1
fi
if [[ "$credential_fill_status" -ne 0 && "$credential_fill_status" -ne 128 ]]; then
    echo "unexpected governed credential fill status: $credential_fill_status" >&2
    exit 1
fi
git -C "$tmp" config --unset credential.helper
git -C "$tmp" config --unset core.askPass

transport_marker="$configured_worktree/local-transport-ran"
transport_helper="$configured_worktree/local-transport"
printf '#!/bin/sh\nprintf local > %q\nexit 1\n' \
    "$transport_marker" > "$transport_helper"
chmod 700 "$transport_helper"
git -C "$tmp" remote add hostile "ext::$transport_helper"
git -C "$tmp" config protocol.ext.allow always
if git_no_replace -C "$tmp" fetch hostile >/dev/null 2>&1; then
    echo "repository-local ext transport was accepted" >&2
    exit 1
fi
if [[ -e "$transport_marker" ]]; then
    echo "repository-local ext transport executed" >&2
    exit 1
fi
git -C "$tmp" remote remove hostile
git -C "$tmp" config --unset protocol.ext.allow

filter_marker="$configured_worktree/local-filter-ran"
git -C "$tmp" config filter.hostile.clean \
    "!f() { printf local > '$filter_marker'; cat; }; f"
if assert_exact_clean_worktree "$tmp" "$commit" >/dev/null 2>&1; then
    echo "repository-local filter command was accepted" >&2
    exit 1
fi
if [[ -e "$filter_marker" ]]; then
    echo "repository-local filter command executed" >&2
    exit 1
fi
git -C "$tmp" config --unset filter.hostile.clean
assert_exact_clean_worktree "$tmp" "$commit"

git -C "$tmp" config tar.hostile.command "$transport_helper"
if assert_no_git_replacement_refs "$tmp" >/dev/null 2>&1; then
    echo "repository-local archive command was accepted" >&2
    exit 1
fi
if [[ -e "$transport_marker" ]]; then
    echo "repository-local archive command executed" >&2
    exit 1
fi
git -C "$tmp" config --unset tar.hostile.command
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
