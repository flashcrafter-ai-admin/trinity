#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="$ROOT/scripts/deploy/backup-persistent-state.sh"
UPGRADE_SCRIPT="$ROOT/scripts/deploy/safe-upgrade.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/backups"
cat > "$TMP/trinity.env" <<'EOF'
SECRET_KEY=fixture-secret
CREDENTIAL_ENCRYPTION_KEY=fixture-encryption
AGENT_AUTH_SECRET=fixture-agent-auth
ADMIN_PASSWORD=fixture-admin
REDIS_PASSWORD=fixture-redis
REDIS_BACKEND_PASSWORD=fixture-backend-redis
EOF

cat > "$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
joined=" $* "
if [[ -n "${FAKE_DOCKER_LOG:-}" ]]; then printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG}"; fi
case "${1:-}" in
  info) exit 0 ;;
  ps)
    if [[ "$joined" == *"com.docker.compose.service=backend"* ]]; then
      echo trinity-backend
    elif [[ "$joined" == *"com.docker.compose.service=postgres"* && "${FAKE_POSTGRES:-0}" == 1 ]]; then
      echo trinity-postgres
    elif [[ "$joined" == *"com.docker.compose.service="* ]]; then
      :
    elif [[ "$joined" == *" -a "* && "$joined" == *" --format {{.Names}} "* ]]; then
      printf '%s\n' "${FAKE_AGENT_CONTAINERS:-}"
    elif [[ "$joined" == *"com.docker.compose.project=trinity"* ]]; then
      echo 'trinity-backend backend:fixture Up'
    fi
    ;;
  volume)
    [[ "${2:-}" == ls ]] || exit 2
    printf '%s\n' "${FAKE_AGENT_VOLUMES:-}"
    ;;
  inspect)
    if [[ "$joined" == *"range .Mounts"* ]]; then
      container=${2:-}
      printf '%s\n' "${FAKE_AGENT_WORKSPACE_MOUNT:-${container}-workspace}"
    elif [[ "$joined" == *"range .Config.Env"* ]]; then
      printf 'DATABASE_URL=%s\n' "${FAKE_DATABASE_URL:-sqlite:////data/trinity.db}"
    elif [[ "$joined" == *"{{.Image}}"* ]]; then
      echo sha256:backendfixture
    elif [[ "$joined" == *"{{.State.Running}}"* ]]; then
      echo true
    else
      exit 2
    fi
    ;;
  pause|unpause|rm) exit 0 ;;
  exec)
    if [[ "$joined" == *" pg_dump "* ]]; then
      printf 'postgres-dump-fixture\n'
    elif [[ "$joined" == *" psql "* ]]; then
      printf 't\n'
    else
      cat >/dev/null || true
    fi
    ;;
  run)
    if [[ "$joined" == *" --detach "* && "$joined" == *" postgres:16-alpine "* ]]; then
      echo trinity-backup-restore-fixture
      exit 0
    fi
    backup=""
    previous=""
    for argument in "$@"; do
      if [[ "$previous" == -v && "$argument" == *:/backup* ]]; then
        backup=${argument%%:/backup*}
      fi
      previous=$argument
    done
    [[ -n "$backup" ]] || exit 2
    if [[ "$joined" == *"source.backup(target)"* ]]; then
      printf 'sqlite-fixture\n' > "$backup/trinity.db"
    elif [[ "$joined" == *"PRAGMA integrity_check"* ]]; then
      [[ "${FAKE_SQLITE_INTEGRITY_FAIL:-0}" == 0 ]]
    elif [[ "$joined" == *"sqlite-data.tgz"* && "$joined" == *"tar -czf"* ]]; then
      tar -czf "$backup/sqlite-data.tgz" -C "$backup" trinity.db
    elif [[ "$joined" == *"backend-data.tgz"* && "$joined" == *"tar -czf"* ]]; then
      fixture=$(mktemp -d)
      printf 'backend-data\n' > "$fixture/state"
      tar -czf "$backup/backend-data.tgz" -C "$fixture" .
      rm -rf "$fixture"
    elif [[ "$joined" == *":/source:ro"* && "$joined" == *"tar -czf"* ]]; then
      archive=${!#}
      fixture=$(mktemp -d)
      printf 'agent-workspace\n' > "$fixture/state"
      tar -czf "$backup/$archive" -C "$fixture" .
      rm -rf "$fixture"
    elif [[ "$joined" == *"source_digest="* && "$joined" == *"restore_digest="* ]]; then
      exit 0
    elif [[ "$joined" == *"tar -tzf"* ]]; then
      archive=$(printf '%s\n' "$joined" | sed -n 's#.* /backup/\([^ ]*\.tgz\).*#\1#p')
      [[ -n "$archive" ]] || archive=$(printf '%s\n' "$joined" | sed -n 's#.* /backup/\([^ ]*\.tar\.gz\).*#\1#p')
      [[ -n "$archive" ]] || exit 2
      tar -tzf "$backup/$archive" >/dev/null
    else
      exit 2
    fi
    ;;
  compose)
    if [[ "$joined" == *" config --services "* ]]; then
      echo backend
    fi
    ;;
  *) exit 2 ;;
esac
EOF
chmod 755 "$TMP/bin/docker"

run_backup() {
  local output=$1
  local result=$2
  PATH="$TMP/bin:$PATH" "$SCRIPT" \
    --project-name trinity \
    --output-dir "$output" \
    --env-file "$TMP/trinity.env" \
    --result-file "$result"
}

result="$TMP/result"
run_backup "$TMP/backups" "$result"
bundle=$(cat "$result")
test -s "$bundle/env.backup"
test -s "$bundle/sqlite-data.tgz"
test -s "$bundle/backend-data.tgz"
grep -qx 'sqlite_backup_verified=yes' "$bundle/manifest.txt"
grep -qx 'backend_data_verified=yes' "$bundle/manifest.txt"
grep -qx 'backend_data_live_consistency_verified=yes' "$bundle/manifest.txt"
grep -qx 'environment_semantics_verified=yes' "$bundle/manifest.txt"
grep -qx 'agent_workspace_live_consistency_verified=yes' "$bundle/manifest.txt"
grep -qx 'backup_complete=yes' "$bundle/manifest.txt"

agent_result="$TMP/agent-result"
FAKE_AGENT_VOLUMES='agent-paid-media-workspace' PATH="$TMP/bin:$PATH" "$SCRIPT" \
  --project-name trinity \
  --output-dir "$TMP/agent-backups" \
  --env-file "$TMP/trinity.env" \
  --result-file "$agent_result" >/dev/null
agent_bundle=$(cat "$agent_result")
test -s "$agent_bundle/agent-workspaces/agent-paid-media-workspace.tgz"
grep -qx 'agent_workspace_archives=1' "$agent_bundle/manifest.txt"
grep -qx 'agent_workspace_archives_verified=yes' "$agent_bundle/manifest.txt"

if FAKE_AGENT_CONTAINERS='agent-paid-media' PATH="$TMP/bin:$PATH" "$SCRIPT" \
  --project-name trinity --output-dir "$TMP/missing-agent-volume" \
  --env-file "$TMP/trinity.env" --result-file "$TMP/missing-agent-result" \
  >/dev/null 2>&1; then
  echo 'agent container without its governed workspace volume was accepted' >&2
  exit 1
fi

if FAKE_AGENT_CONTAINERS='agent-paid-media' \
  FAKE_AGENT_VOLUMES='agent-paid-media-workspace' \
  FAKE_AGENT_WORKSPACE_MOUNT='agent-wrong-workspace' \
  PATH="$TMP/bin:$PATH" "$SCRIPT" \
  --project-name trinity --output-dir "$TMP/wrong-agent-mount" \
  --env-file "$TMP/trinity.env" --result-file "$TMP/wrong-agent-mount-result" \
  >/dev/null 2>&1; then
  echo 'agent container with the wrong mounted workspace was accepted' >&2
  exit 1
fi

partial_result="$TMP/partial-result"
PATH="$TMP/bin:$PATH" "$SCRIPT" --project-name trinity --output-dir "$TMP/partial" \
  --env-file "$TMP/trinity.env" --result-file "$partial_result" \
  --skip-agent-workspaces >/dev/null
partial_bundle=$(cat "$partial_result")
grep -qx 'backup_complete=no' "$partial_bundle/manifest.txt"
grep -qx 'backup_partial=yes' "$partial_bundle/manifest.txt"
! grep -qx 'backup_complete=yes' "$partial_bundle/manifest.txt"

rm -f "$result"
if FAKE_SQLITE_INTEGRITY_FAIL=1 run_backup "$TMP/corrupt" "$result" >/dev/null 2>&1; then
  echo 'corrupt SQLite snapshot was accepted' >&2
  exit 1
fi
test ! -e "$result"
! grep -Rqx 'backup_complete=yes' "$TMP/corrupt" 2>/dev/null

if PATH="$TMP/bin:$PATH" "$SCRIPT" --project-name trinity --output-dir "$TMP/no-env" \
  --env-file "$TMP/missing.env" --result-file "$result" --skip-agent-workspaces \
  >/dev/null 2>&1; then
  echo 'missing environment backup was accepted' >&2
  exit 1
fi

if FAKE_DATABASE_URL='postgresql://managed.example/trinity' run_backup \
  "$TMP/external-db" "$result" >/dev/null 2>&1; then
  echo 'external PostgreSQL without a governed snapshot was accepted' >&2
  exit 1
fi

external_url='postgresql://managed.example/trinity'
if command -v sha256sum >/dev/null 2>&1; then
  external_digest="sha256:$(printf '%s' "$external_url" | sha256sum | awk '{print $1}')"
else
  external_digest="sha256:$(printf '%s' "$external_url" | shasum -a 256 | awk '{print $1}')"
fi
cat > "$TMP/external-receipt.json" <<EOF
{"schemaVersion":"trinity-external-database-snapshot/1","databaseUrlDigest":"$external_digest","provider":"managed-db","snapshotId":"snapshot:fixture-1","createdAt":"2026-07-16T07:00:00Z"}
EOF
if FAKE_DATABASE_URL="$external_url" PATH="$TMP/bin:$PATH" "$SCRIPT" \
  --project-name trinity --output-dir "$TMP/external-unsigned" \
  --env-file "$TMP/trinity.env" --external-db-snapshot-receipt "$TMP/external-receipt.json" \
  --result-file "$TMP/external-result" >/dev/null 2>&1; then
  echo 'legacy unsigned external database receipt was accepted' >&2
  exit 1
fi

postgres_result="$TMP/postgres-result"
FAKE_POSTGRES=1 FAKE_DATABASE_URL='postgresql://postgres/trinity' \
FAKE_DOCKER_LOG="$TMP/postgres-docker.log" PATH="$TMP/bin:$PATH" "$SCRIPT" \
  --project-name trinity --output-dir "$TMP/postgres" --env-file "$TMP/trinity.env" \
  --result-file "$postgres_result" >/dev/null
postgres_bundle=$(cat "$postgres_result")
grep -qx 'database_source=bundled-postgres' "$postgres_bundle/manifest.txt"
grep -qx 'postgres_dump_verified=yes' "$postgres_bundle/manifest.txt"
grep -qx 'postgres_restore_verified=yes' "$postgres_bundle/manifest.txt"
grep -q 'pg_restore.*trinity_restore' "$TMP/postgres-docker.log"

if "$UPGRADE_SCRIPT" --skip-backup --dry-run >/dev/null 2>&1; then
  echo 'safe upgrade still permits bypassing its backup gate' >&2
  exit 1
fi
grep -q "external_postgres_snapshot_verified=yes" "$UPGRADE_SCRIPT"
grep -q "external_postgres_provider_verified=yes" "$UPGRADE_SCRIPT"
grep -q "postgres_restore_verified=yes" "$UPGRADE_SCRIPT"
grep -q "environment_semantics_verified=yes" "$UPGRADE_SCRIPT"
grep -q "agent_workspace_archives_verified=yes" "$UPGRADE_SCRIPT"
grep -q "Version endpoint was not reachable" "$UPGRADE_SCRIPT"
! grep -q 'api/version.*|| true' "$UPGRADE_SCRIPT"

printf 'services: {}\n' > "$TMP/compose.yml"
upgrade_plan=$(FAKE_AGENT_VOLUMES='agent-paid-media-workspace' PATH="$TMP/bin:$PATH" \
  "$UPGRADE_SCRIPT" --allow-fresh --dry-run -f "$TMP/compose.yml")
printf '%s\n' "$upgrade_plan" | grep -q 'backup-persistent-state.sh'
if printf '%s\n' "$upgrade_plan" | grep -q 'First install has no persistent state'; then
  echo 'existing agent workspace was classified as a fresh install' >&2
  exit 1
fi

governed="$TMP/governed"
mkdir -p "$governed/scripts/deploy"
cp "$ROOT/scripts/deploy/safe-upgrade.sh" "$governed/scripts/deploy/safe-upgrade.sh"
cp "$ROOT/scripts/deploy/backup-persistent-state.sh" "$governed/scripts/deploy/backup-persistent-state.sh"
cp "$ROOT/scripts/deploy/github-actions-safe-deploy.sh" "$governed/scripts/deploy/github-actions-safe-deploy.sh"
chmod 755 "$governed/scripts/deploy/"*.sh
printf 'services:\n  backend:\n    build:\n      context: .\n' > "$governed/docker-compose.yml"
git -C "$governed" init -q
git -C "$governed" config user.email test@example.com
git -C "$governed" config user.name test
git -C "$governed" add .
git -C "$governed" commit -qm governed
governed_commit=$(git -C "$governed" rev-parse HEAD)
governed_plan=$(TRINITY_EXPECTED_SOURCE_REVISION="$governed_commit" \
  GIT_COMMIT="$governed_commit" FAKE_AGENT_VOLUMES='agent-paid-media-workspace' \
  PATH="$TMP/bin:$PATH" "$governed/scripts/deploy/safe-upgrade.sh" \
  --allow-fresh --dry-run --env-file "$TMP/trinity.env" \
  -f "$governed/docker-compose.yml")
printf '%s\n' "$governed_plan" | grep -q "Prepared exact-commit build source $governed_commit"
printf '%s\n' "$governed_plan" | grep -q 'trinity-release-inputs.*source.*build'
printf '%s\n' "$governed_plan" | grep -q -- '--env-file .*trinity-release-inputs.*config/runtime.env'
printf '%s\n' "$governed_plan" | grep -q 'up --no-build -d backend'

echo 'backup-persistent-state: OK'
