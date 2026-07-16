#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="$ROOT/scripts/deploy/backup-persistent-state.sh"
UPGRADE_SCRIPT="$ROOT/scripts/deploy/safe-upgrade.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/backups"
printf 'ADMIN_PASSWORD=fixture\n' > "$TMP/trinity.env"

cat > "$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
joined=" $* "
case "${1:-}" in
  info) exit 0 ;;
  ps)
    if [[ "$joined" == *"com.docker.compose.service=backend"* ]]; then
      echo trinity-backend
    elif [[ "$joined" == *"com.docker.compose.service="* ]]; then
      :
    elif [[ "$joined" == *"com.docker.compose.project=trinity"* ]]; then
      echo 'trinity-backend backend:fixture Up'
    fi
    ;;
  volume)
    [[ "${2:-}" == ls ]] || exit 2
    ;;
  inspect)
    if [[ "$joined" == *"range .Config.Env"* ]]; then
      printf 'DATABASE_URL=%s\n' "${FAKE_DATABASE_URL:-sqlite:////data/trinity.db}"
    elif [[ "$joined" == *"{{.Image}}"* ]]; then
      echo sha256:backendfixture
    elif [[ "$joined" == *"{{.State.Running}}"* ]]; then
      echo true
    else
      exit 2
    fi
    ;;
  run)
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
    elif [[ "$joined" == *"tar -tzf"* ]]; then
      archive=$(printf '%s\n' "$joined" | sed -n 's#.* /backup/\([^ ]*\.tgz\).*#\1#p')
      [[ -n "$archive" ]] || archive=$(printf '%s\n' "$joined" | sed -n 's#.* /backup/\([^ ]*\.tar\.gz\).*#\1#p')
      [[ -n "$archive" ]] || exit 2
      tar -tzf "$backup/$archive" >/dev/null
    else
      exit 2
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
    --result-file "$result" \
    --skip-agent-workspaces
}

result="$TMP/result"
run_backup "$TMP/backups" "$result"
bundle=$(cat "$result")
test -s "$bundle/env.backup"
test -s "$bundle/sqlite-data.tgz"
test -s "$bundle/backend-data.tgz"
grep -qx 'sqlite_backup_verified=yes' "$bundle/manifest.txt"
grep -qx 'backend_data_verified=yes' "$bundle/manifest.txt"
grep -qx 'backup_complete=yes' "$bundle/manifest.txt"

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
external_result="$TMP/external-result"
FAKE_DATABASE_URL="$external_url" PATH="$TMP/bin:$PATH" "$SCRIPT" \
  --project-name trinity \
  --output-dir "$TMP/external-valid" \
  --env-file "$TMP/trinity.env" \
  --external-db-snapshot-receipt "$TMP/external-receipt.json" \
  --result-file "$external_result" \
  --skip-agent-workspaces >/dev/null
external_bundle=$(cat "$external_result")
test -s "$external_bundle/external-postgres-snapshot.json"
grep -qx 'external_postgres_snapshot_verified=yes' "$external_bundle/manifest.txt"
grep -qx 'backup_complete=yes' "$external_bundle/manifest.txt"

if "$UPGRADE_SCRIPT" --skip-backup --dry-run >/dev/null 2>&1; then
  echo 'safe upgrade still permits bypassing its backup gate' >&2
  exit 1
fi
grep -q "external_postgres_snapshot_verified=yes" "$UPGRADE_SCRIPT"
grep -q "Version endpoint was not reachable" "$UPGRADE_SCRIPT"
! grep -q 'api/version.*|| true' "$UPGRADE_SCRIPT"

echo 'backup-persistent-state: OK'
