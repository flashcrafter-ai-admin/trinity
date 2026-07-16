#!/usr/bin/env bash
#
# Back up the persistent state of a running Trinity instance.
#
# This script is intentionally conservative:
#   - it discovers the running compose project by labels,
#   - it takes a logical PostgreSQL dump when a bundled postgres service exists,
#   - it briefly pauses writers while archiving backend and agent workspaces,
#   - it never stops or removes production containers or volumes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
ARCHIVE_RESTORE_VERIFIER="${SCRIPT_DIR}/verify-archive-restore.sh"
# shellcheck source=github-actions-safe-deploy.sh
source "${SCRIPT_DIR}/github-actions-safe-deploy.sh"

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-trinity}"
OUTPUT_DIR="${PROJECT_ROOT}/backups/persistent-state"
ENV_FILE="${PROJECT_ROOT}/.env"
INCLUDE_AGENT_WORKSPACES=1
INCLUDE_BACKEND_DATA=1
INCLUDE_ENV=1
EXTERNAL_DB_SNAPSHOT_RECEIPT=""
EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY=""
EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND=""
RESULT_FILE=""
PAUSED_CONTAINERS=()
REQUIRED_PAUSED_CONTAINERS=()
POSTGRES_VERIFY_CONTAINER=""

usage() {
  cat <<'EOF'
Usage: scripts/deploy/backup-persistent-state.sh [options]

Options:
  --project-name NAME              Docker Compose project name (default: trinity)
  --output-dir DIR                 Backup parent directory (default: ./backups/persistent-state)
  --env-file FILE                  Env file to copy into the bundle (default: ./.env)
  --skip-agent-workspaces          Do not archive agent-*-workspace Docker volumes
  --skip-backend-data              Do not archive the backend /data mount
  --skip-env                       Do not copy .env into the backup bundle
  --external-db-snapshot-receipt FILE
                                   JSON receipt for an already-completed managed PostgreSQL snapshot
  --external-db-snapshot-public-key FILE
                                   Root-owned Ed25519 public key authorizing external receipts
  --external-db-snapshot-verify-command FILE
                                   Root-owned provider verifier for the signed snapshot receipt
  --result-file FILE               Write the completed backup directory to FILE
  -h, --help                       Show this help

The backup directory contains secrets if .env is copied. It is chmod 700 and
must remain outside git.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      PROJECT_NAME="${2:?--project-name requires a value}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:?--output-dir requires a value}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?--env-file requires a value}"
      shift 2
      ;;
    --skip-agent-workspaces)
      INCLUDE_AGENT_WORKSPACES=0
      shift
      ;;
    --skip-backend-data)
      INCLUDE_BACKEND_DATA=0
      shift
      ;;
    --skip-env)
      INCLUDE_ENV=0
      shift
      ;;
    --external-db-snapshot-receipt)
      EXTERNAL_DB_SNAPSHOT_RECEIPT="${2:?--external-db-snapshot-receipt requires a value}"
      shift 2
      ;;
    --external-db-snapshot-public-key)
      EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY="${2:?--external-db-snapshot-public-key requires a value}"
      shift 2
      ;;
    --external-db-snapshot-verify-command)
      EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND="${2:?--external-db-snapshot-verify-command requires a value}"
      shift 2
      ;;
    --result-file)
      RESULT_FILE="${2:?--result-file requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[backup] %s\n' "$*"
}

warn() {
  printf '[backup] WARN: %s\n' "$*" >&2
}

die() {
  printf '[backup] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

cleanup() {
  local container paused_snapshot="${PAUSED_CONTAINERS[*]-}"
  for container in ${paused_snapshot}; do
    docker unpause "${container}" >/dev/null 2>&1 || true
  done
  if [[ -n "${POSTGRES_VERIFY_CONTAINER}" ]]; then
    docker rm -f "${POSTGRES_VERIFY_CONTAINER}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

pause_container() {
  local container="$1" existing required=0
  if container_running "${container}"; then
    for existing in ${REQUIRED_PAUSED_CONTAINERS[*]-}; do
      [[ "${existing}" == "${container}" ]] && required=1
    done
    if [[ ${required} -eq 0 ]]; then
      REQUIRED_PAUSED_CONTAINERS+=("${container}")
    fi
    if [[ "$(docker inspect --format '{{.State.Paused}}' "${container}")" != true ]]; then
      docker pause "${container}" >/dev/null
      PAUSED_CONTAINERS+=("${container}")
    fi
  fi
}

agent_volume_inventory() {
  docker volume ls --format '{{.Name}}' | grep -E '^agent-.+-workspace$' | sort || true
}

agent_container_inventory() {
  docker ps -a --no-trunc --format '{{.ID}} {{.Names}} {{.Image}}' \
    | awk '$2 ~ /^agent-.+/' | sort || true
}

compose_named_volume_inventory() {
  local container
  {
    # Compose labels survive container removal, so detached project volumes must
    # be discovered independently from the extant container mount graph.
    docker volume ls \
      --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
      --format '{{.Name}}'
    docker volume ls --format '{{.Name}}' \
      | awk -v prefix="${PROJECT_NAME}_" 'index($0, prefix) == 1'
    while IFS= read -r container; do
      [[ -n "${container}" ]] || continue
      docker inspect "${container}" \
        --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}'
    done < <(docker ps -a \
      --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
      --format '{{.Names}}')
  } | sed '/^$/d' | sort -u
}

assert_agent_inventory_unchanged() {
  [[ "$(agent_volume_inventory)" == "${AGENT_VOLUME_INVENTORY}" ]] \
    || die "Agent workspace volume inventory changed during backup"
  [[ "$(agent_container_inventory)" == "${AGENT_CONTAINER_INVENTORY}" ]] \
    || die "Agent container inventory changed during backup"
}

assert_writers_still_paused() {
  local container
  for container in ${REQUIRED_PAUSED_CONTAINERS[*]-}; do
    [[ "$(docker inspect --format '{{.State.Running}}:{{.State.Paused}}' "${container}")" == true:true ]] \
      || die "Writer ${container} resumed or changed state during backup"
  done
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

sha256_text() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  else
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  fi
}

validate_environment_file() {
  local file="$1"
  local key count value
  for key in \
    SECRET_KEY \
    CREDENTIAL_ENCRYPTION_KEY \
    AGENT_AUTH_SECRET \
    ADMIN_PASSWORD \
    REDIS_PASSWORD \
    REDIS_BACKEND_PASSWORD; do
    count=$(grep -c "^${key}=" "${file}" || true)
    [[ "${count}" == 1 ]] || die "Environment backup must contain exactly one ${key}"
    value=$(sed -n "s/^${key}=//p" "${file}")
    value="${value//[[:space:]]/}"
    [[ -n "${value}" && "${value}" != '""' && "${value}" != "''" ]] \
      || die "Environment backup contains an empty ${key}"
  done
}

service_container() {
  docker ps -a \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=$1" \
    --format '{{.Names}}' \
    | head -n 1
}

container_running() {
  [[ "$(docker inspect --format '{{.State.Running}}' "$1")" == "true" ]]
}

verify_archive() {
  local archive_dir="$1"
  local archive_name="$2"
  [[ -s "${archive_dir}/${archive_name}" ]] || die "Archive is missing or empty: ${archive_name}"
  docker run --rm \
    -v "${archive_dir}:/backup:ro" \
    alpine:3.20 \
    tar -tzf "/backup/${archive_name}" >/dev/null \
    || die "Archive verification failed: ${archive_name}"
}

verify_archive_against_volume() {
  local volume="$1"
  local archive_dir="$2"
  local archive_name="$3"
  docker run --rm \
    -v "${volume}:/source:ro" \
    -v "${archive_dir}:/backup:ro" \
    -v "${ARCHIVE_RESTORE_VERIFIER}:/verify-archive-restore.sh:ro" \
    --tmpfs /restore:rw,nosuid,nodev \
    alpine:3.20 \
    sh -ec '
      mkdir -p /restore/tree
      tar -xzf "/backup/$1" -C /restore/tree
      sh /verify-archive-restore.sh /source /restore/tree
    ' sh "${archive_name}" \
    || die "Archive content does not exactly restore the live volume: ${volume}"
}

archive_volume() {
  local volume="$1"
  local archive_dir="$2"
  local archive_name="$3"

  mkdir -p "${archive_dir}"
  docker run --rm \
    -v "${volume}:/source:ro" \
    -v "${archive_dir}:/backup" \
    alpine:3.20 \
    sh -c 'cd /source && tar -czf "/backup/$1" .' sh "${archive_name}"
  verify_archive "${archive_dir}" "${archive_name}"
  verify_archive_against_volume "${volume}" "${archive_dir}" "${archive_name}"
}

capture_postgres_inventory() {
  local container="$1" user="$2" database="$3" output="$4"
  docker exec -i "${container}" sh -ec \
    'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -X -v ON_ERROR_STOP=1 -U "$1" -d "$2" -Atq' \
    sh "${user}" "${database}" > "${output}" <<'SQL'
SET TIME ZONE 'UTC';
SET bytea_output = 'hex';
SET DateStyle = 'ISO, YMD';
SET IntervalStyle = 'iso_8601';
SET extra_float_digits = 3;
SELECT 'column:' || encode(convert_to(jsonb_build_array(n.nspname, c.relname,
  a.attname, a.attnum::text, pg_catalog.format_type(a.atttypid, a.atttypmod),
  a.attnotnull::text, coalesce(pg_get_expr(d.adbin, d.adrelid), ''))::text, 'UTF8'), 'hex')
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r','p')
  AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname, a.attnum;
SELECT 'constraint:' || encode(convert_to(jsonb_build_array(n.nspname, c.relname,
  x.conname, x.contype, pg_get_constraintdef(x.oid, true))::text, 'UTF8'), 'hex')
FROM pg_catalog.pg_constraint x
JOIN pg_catalog.pg_class c ON c.oid = x.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname, x.conname;
SELECT 'index:' || encode(convert_to(jsonb_build_array(n.nspname, c.relname,
  i.relname, pg_get_indexdef(i.oid))::text, 'UTF8'), 'hex')
FROM pg_catalog.pg_index x
JOIN pg_catalog.pg_class c ON c.oid = x.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid
WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname, i.relname;
SELECT 'function:' || encode(convert_to(jsonb_build_array(
  n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
  p.prokind, pg_get_functiondef(p.oid))::text, 'UTF8'), 'hex')
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f','p')
  AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid);
SELECT 'trigger:' || encode(convert_to(jsonb_build_array(
  n.nspname, c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid, true))::text,
  'UTF8'), 'hex')
FROM pg_catalog.pg_trigger t
JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal
  AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname, t.tgname;
SELECT format('SELECT %L || E''\t'' || count(*)::text FROM %I.%I;',
  'rows:' || encode(convert_to(n.nspname || '.' || c.relname, 'UTF8'), 'hex'),
  n.nspname, c.relname)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname;
\gexec
SELECT format(
  'SELECT %L || E''\t'' || encode(convert_to(row_json, ''UTF8''), ''hex'') '
  'FROM (SELECT to_jsonb(source_row)::text AS row_json FROM %I.%I AS source_row) AS canonical_rows '
  'ORDER BY row_json COLLATE "C";',
  'data:' || encode(convert_to(n.nspname || '.' || c.relname, 'UTF8'), 'hex'),
  n.nspname, c.relname)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname;
\gexec
SELECT format('SELECT %L || E''\t'' || last_value::text || E''\t'' || is_called::text FROM %I.%I;',
  'sequence:' || encode(convert_to(n.nspname || '.' || c.relname, 'UTF8'), 'hex'),
  n.nspname, c.relname)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S' AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname !~ '^pg_toast'
ORDER BY n.nspname, c.relname;
\gexec
SQL
  [[ -s "${output}" ]] || die "PostgreSQL application inventory is empty"
}

verify_postgres_dump_restore() {
  local dump="$1"
  local source_catalog="$2"
  POSTGRES_VERIFY_CONTAINER="trinity-backup-restore-${PROJECT_NAME}-$$"
  docker run --detach --name "${POSTGRES_VERIFY_CONTAINER}" \
    --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev \
    --env POSTGRES_PASSWORD=restore-verification-only \
    postgres:16-alpine >/dev/null
  local ready=0
  for _ in $(seq 1 60); do
    if docker exec "${POSTGRES_VERIFY_CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  [[ ${ready} -eq 1 ]] || die "Fresh PostgreSQL restore verifier did not become ready"
  docker exec "${POSTGRES_VERIFY_CONTAINER}" createdb -U postgres trinity_restore
  docker exec -i "${POSTGRES_VERIFY_CONTAINER}" pg_restore \
    --exit-on-error --no-owner --no-privileges -U postgres -d trinity_restore < "${dump}" \
    || die "PostgreSQL dump could not be restored into a fresh database"
  capture_postgres_inventory "${POSTGRES_VERIFY_CONTAINER}" postgres trinity_restore \
    "${RUN_DIR}/postgres-restore-catalog.txt"
  cmp -s "${source_catalog}" "${RUN_DIR}/postgres-restore-catalog.txt" \
    || die "Restored PostgreSQL schema or row content differs from the paused source"
  docker rm -f "${POSTGRES_VERIFY_CONTAINER}" >/dev/null
  POSTGRES_VERIFY_CONTAINER=""
}

validate_external_snapshot_receipt() {
  local database_url="$1"
  require_cmd jq
  require_cmd python3
  require_cmd openssl
  [[ -s "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" && ! -L "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" ]] \
    || die "External PostgreSQL requires a signed snapshot receipt"
  [[ "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}" == /* && -f "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}" ]] \
    || die "External PostgreSQL requires an absolute snapshot public key path"
  [[ "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}" == /* && -x "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}" ]] \
    || die "External PostgreSQL requires an absolute executable provider verifier"
  python3 - "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}" "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}" <<'PY'
import os
import shlex
import stat
import sys

def require_root_controlled(raw):
    path = os.path.normpath(raw)
    current = "/"
    for part in path.split(os.sep)[1:]:
        current = os.path.join(current, part)
        value = os.lstat(current)
        if stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise SystemExit(f"external verification asset is not root-controlled: {current}")
    final = os.lstat(path)
    if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
        raise SystemExit(f"external verification asset is not a one-link regular file: {path}")

for raw in sys.argv[1:]:
    require_root_controlled(raw)

verifier = sys.argv[2]
with open(verifier, "rb") as source:
    first_line = source.readline(4096)
if first_line.startswith(b"#!"):
    try:
        command = shlex.split(first_line[2:].decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit("external provider verifier has an invalid shebang") from exc
    if not command or not os.path.isabs(command[0]) or command[0] == "/usr/bin/env":
        raise SystemExit("external provider verifier must use a direct root-controlled interpreter")
    require_root_controlled(command[0])
PY

  local public_key_digest verifier_digest receipt
  public_key_digest="sha256:$(sha256_file "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}")"
  verifier_digest="sha256:$(sha256_file "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}")"
  receipt="${RUN_DIR}/external-postgres-snapshot.json"
  cp "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" "${receipt}"
  chmod 600 "${receipt}"
  local database_url_digest="sha256:$(sha256_text "${database_url}")"
  jq -e --arg digest "${database_url_digest}" '
    (keys | sort) == ["createdAt","databaseUrlDigest","expiresAt","provider","schemaVersion","signature","snapshotId"]
    and .schemaVersion == "trinity-external-database-snapshot/2"
    and .databaseUrlDigest == $digest
    and (.provider | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$"))
    and (.snapshotId | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$"))
    and (.signature | type == "string" and test("^[A-Za-z0-9+/]+={0,2}$"))
    and (.createdAt | type == "string")
    and (.expiresAt | type == "string")
  ' "${receipt}" >/dev/null \
    || die "External PostgreSQL snapshot receipt is malformed or bound to another database"
  python3 - "${receipt}" <<'PY'
import datetime as dt
import json
import sys

receipt = json.load(open(sys.argv[1], encoding="utf-8"))
now = dt.datetime.now(dt.timezone.utc)
created = dt.datetime.fromisoformat(receipt["createdAt"].replace("Z", "+00:00"))
expires = dt.datetime.fromisoformat(receipt["expiresAt"].replace("Z", "+00:00"))
if created > now + dt.timedelta(minutes=5) or expires <= now or expires - created > dt.timedelta(hours=1):
    raise SystemExit("external snapshot receipt is stale or has an invalid validity window")
PY

  local canonical signature_file provider_attestation signed_receipt_digest
  canonical="${RUN_DIR}/external-receipt.canonical.json"
  signature_file="${RUN_DIR}/external-receipt.signature"
  jq -cS 'del(.signature)' "${receipt}" > "${canonical}"
  python3 - "${receipt}" "${signature_file}" <<'PY'
import base64
import json
import sys

receipt = json.load(open(sys.argv[1], encoding="utf-8"))
open(sys.argv[2], "wb").write(base64.b64decode(receipt["signature"], validate=True))
PY
  openssl pkeyutl -verify -pubin -inkey "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}" \
    -rawin -in "${canonical}" -sigfile "${signature_file}" >/dev/null \
    || die "External PostgreSQL snapshot receipt signature is invalid"
  rm -f "${canonical}" "${signature_file}"
  signed_receipt_digest="sha256:$(sha256_file "${receipt}")"

  provider_attestation="${RUN_DIR}/external-provider-verification.json"
  rm -f "${provider_attestation}"
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/var/empty LANG=C.UTF-8 \
    "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}" \
    "${receipt}" "${provider_attestation}"
  [[ "sha256:$(sha256_file "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}")" == "${public_key_digest}" ]] \
    || die "External snapshot public key changed during verification"
  [[ "sha256:$(sha256_file "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}")" == "${verifier_digest}" ]] \
    || die "External provider verifier changed during verification"
  [[ -s "${provider_attestation}" && ! -L "${provider_attestation}" ]] \
    || die "External provider verifier returned no regular attestation"
  [[ "$(stat -c '%h' "${provider_attestation}")" == 1 ]] \
    || die "External provider verifier returned a linked attestation"
  [[ "sha256:$(sha256_file "${receipt}")" == "${signed_receipt_digest}" ]] \
    || die "External provider verifier altered the signed receipt"
  jq -e --arg receipt "${signed_receipt_digest}" \
    --arg provider "$(jq -r .provider "${receipt}")" \
    --arg snapshot "$(jq -r .snapshotId "${receipt}")" '
    (keys | sort) == ["provider","receiptSha256","schemaVersion","snapshotId","verifiedAt"]
    and .schemaVersion == "trinity-external-database-provider-verification/1"
    and .receiptSha256 == $receipt
    and .provider == $provider
    and .snapshotId == $snapshot
    and (.verifiedAt | type == "string")
  ' "${provider_attestation}" >/dev/null \
    || die "External provider verification is not bound to the signed receipt"
  python3 - "${provider_attestation}" <<'PY'
import datetime as dt
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))["verifiedAt"]
verified = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
now = dt.datetime.now(dt.timezone.utc)
if abs((now - verified).total_seconds()) > 300:
    raise SystemExit("provider verification is not fresh")
PY
  chmod 600 "${provider_attestation}"
  {
    echo "external_snapshot_receipt_sha256=${signed_receipt_digest}"
    echo "external_snapshot_public_key_sha256=${public_key_digest}"
    echo "external_snapshot_verifier_sha256=${verifier_digest}"
    echo "external_provider_verification_sha256=sha256:$(sha256_file "${provider_attestation}")"
  } >> "${MANIFEST}"
}

[[ -f "${ARCHIVE_RESTORE_VERIFIER}" ]] \
  || die "Archive restore verifier is missing"
require_cmd docker
docker info >/dev/null 2>&1 || die "Docker is not running"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${OUTPUT_DIR%/}/${TIMESTAMP}-${PROJECT_NAME}"
mkdir -p "${OUTPUT_DIR}"
mkdir "${RUN_DIR}" || die "Backup bundle path already exists: ${RUN_DIR}"
chmod 700 "${RUN_DIR}"

MANIFEST="${RUN_DIR}/manifest.txt"
BACKEND_CONTAINER="$(service_container backend || true)"
SCHEDULER_CONTAINER="$(service_container scheduler || true)"
POSTGRES_CONTAINER="$(service_container postgres || true)"
REDIS_CONTAINER="$(service_container redis || true)"
VECTOR_CONTAINER="$(service_container vector || true)"
COMPOSE_NAMED_VOLUME_INVENTORY="$(compose_named_volume_inventory)"
AGENT_WORKSPACE_VOLUMES=()
AGENT_CONTAINERS=()
AGENT_WORKSPACE_VOLUME_COUNT=0
AGENT_CONTAINER_COUNT=0
if [[ ${INCLUDE_AGENT_WORKSPACES} -eq 1 ]]; then
  AGENT_VOLUME_INVENTORY="$(agent_volume_inventory)"
  AGENT_CONTAINER_INVENTORY="$(agent_container_inventory)"
  while IFS= read -r volume; do
    if [[ -n "${volume}" ]]; then
      AGENT_WORKSPACE_VOLUMES+=("${volume}")
      AGENT_WORKSPACE_VOLUME_COUNT=$((AGENT_WORKSPACE_VOLUME_COUNT + 1))
    fi
  done < <(printf '%s\n' "${AGENT_VOLUME_INVENTORY}")
  while IFS= read -r container; do
    if [[ -n "${container}" ]]; then
      AGENT_CONTAINERS+=("${container}")
      AGENT_CONTAINER_COUNT=$((AGENT_CONTAINER_COUNT + 1))
    fi
  done < <(docker ps -a --format '{{.Names}}' | grep -E '^agent-.+' | sort || true)
  for container in ${AGENT_CONTAINERS[*]-}; do
    expected_volume="${container}-workspace"
    if ! printf '%s\n' ${AGENT_WORKSPACE_VOLUMES[*]-} | grep -Fxq "${expected_volume}"; then
      die "Agent container ${container} has no governed ${expected_volume} workspace volume"
    fi
    mounted_workspace="$(docker inspect "${container}" --format '{{range .Mounts}}{{if eq .Destination "/home/developer"}}{{println .Name}}{{end}}{{end}}' \
      | sed '/^$/d')"
    [[ "${mounted_workspace}" == "${expected_volume}" ]] \
      || die "Agent container ${container} does not mount ${expected_volume} at /home/developer"
  done
else
  AGENT_VOLUME_INVENTORY="$(agent_volume_inventory)"
  AGENT_CONTAINER_INVENTORY="$(agent_container_inventory)"
fi

BACKUP_SOURCE_REVISION="${TRINITY_EXPECTED_SOURCE_REVISION:-}"
if [[ -n "${BACKUP_SOURCE_REVISION}" ]]; then
  [[ "${BACKUP_SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
    || die "TRINITY_EXPECTED_SOURCE_REVISION must be an exact lowercase commit SHA"
else
  BACKUP_SOURCE_REVISION="$(git_no_replace -C "${PROJECT_ROOT}" rev-parse HEAD 2>/dev/null)" \
    || die "Backup source revision cannot be verified"
  [[ "${BACKUP_SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
    || die "Backup source revision is not an exact lowercase commit SHA"
fi

{
  echo "trinity_persistent_state_backup=1"
  echo "timestamp_utc=${TIMESTAMP}"
  echo "compose_project=${PROJECT_NAME}"
  echo "git_head=${BACKUP_SOURCE_REVISION}"
  echo "backup_host=$(hostname 2>/dev/null || echo unknown)"
  echo "backend_container=${BACKEND_CONTAINER:-missing}"
  echo "scheduler_container=${SCHEDULER_CONTAINER:-missing}"
  echo "postgres_container=${POSTGRES_CONTAINER:-missing}"
  echo "redis_container=${REDIS_CONTAINER:-missing}"
  echo "vector_container=${VECTOR_CONTAINER:-missing}"
  echo "env_file=${ENV_FILE}"
  echo "contains_secrets=$([[ ${INCLUDE_ENV} -eq 1 && -s "${ENV_FILE}" ]] && echo yes || echo no)"
  echo
  echo "docker_volumes:"
  docker volume ls --format '{{.Name}}' | sort | sed 's/^/  - /'
  echo
  echo "compose_containers:"
  docker ps -a \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --format '  - {{.Names}} {{.Image}} {{.Status}}'
} > "${MANIFEST}"

log "Writing backup bundle: ${RUN_DIR}"

[[ -n "${BACKEND_CONTAINER}" ]] || die "No backend container exists for compose project ${PROJECT_NAME}"

if [[ ${INCLUDE_ENV} -eq 1 ]]; then
  if [[ -s "${ENV_FILE}" ]]; then
    cp "${ENV_FILE}" "${RUN_DIR}/env.backup"
    chmod 600 "${RUN_DIR}/env.backup"
    validate_environment_file "${RUN_DIR}/env.backup"
    echo "environment_semantics_verified=yes" >> "${MANIFEST}"
    echo "environment_backup_sha256=sha256:$(sha256_file "${RUN_DIR}/env.backup")" >> "${MANIFEST}"
    log "Copied ${ENV_FILE} to env.backup"
  else
    die "Env file is missing or empty at ${ENV_FILE}; refusing an unrecoverable credential backup"
  fi
fi

container_running "${BACKEND_CONTAINER}" \
  || die "Backend container ${BACKEND_CONTAINER} is stopped; no consistent backup is possible"
pause_container "${BACKEND_CONTAINER}"
if [[ -n "${SCHEDULER_CONTAINER}" ]]; then
  pause_container "${SCHEDULER_CONTAINER}"
fi
if [[ -n "${REDIS_CONTAINER}" ]]; then
  pause_container "${REDIS_CONTAINER}"
fi
if [[ -n "${VECTOR_CONTAINER}" ]]; then
  pause_container "${VECTOR_CONTAINER}"
fi
for container in ${AGENT_CONTAINERS[*]-}; do
  pause_container "${container}"
done
assert_agent_inventory_unchanged
assert_writers_still_paused
echo "writer_pause_verified=yes" >> "${MANIFEST}"
echo "paused_writer_count=${#PAUSED_CONTAINERS[@]}" >> "${MANIFEST}"
echo "agent_volume_inventory_sha256=sha256:$(sha256_text "${AGENT_VOLUME_INVENTORY}")" >> "${MANIFEST}"
echo "agent_container_inventory_sha256=sha256:$(sha256_text "${AGENT_CONTAINER_INVENTORY}")" >> "${MANIFEST}"

DATABASE_URL="$(docker inspect "${BACKEND_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^DATABASE_URL=//p' | head -n 1)"
TRINITY_DB_PATH="$(docker inspect "${BACKEND_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^TRINITY_DB_PATH=//p' | head -n 1)"

DATABASE_SOURCE="sqlite"
if [[ "${DATABASE_URL}" == postgresql://* || "${DATABASE_URL}" == postgres://* ]]; then
  require_cmd python3
  POSTGRES_IDENTITY=()
  while IFS= read -r field; do
    POSTGRES_IDENTITY+=("${field}")
  done < <(printf '%s' "${DATABASE_URL}" | python3 -c '
import hashlib, sys
from urllib.parse import parse_qsl, unquote, urlsplit
value = urlsplit(sys.stdin.read())
if value.scheme not in {"postgres", "postgresql"} or not value.hostname or value.fragment:
    raise SystemExit("PostgreSQL DATABASE_URL is invalid")
try:
    query = parse_qsl(value.query, keep_blank_values=True, strict_parsing=True)
except ValueError as exc:
    raise SystemExit("PostgreSQL DATABASE_URL query is invalid") from exc
authority_options = {
    "database", "dbname", "host", "hostaddr", "passfile", "password", "port",
    "service", "servicefile", "user",
}
if any(key.lower() in authority_options for key, _value in query):
    raise SystemExit("PostgreSQL DATABASE_URL query cannot override connection authority")
username = unquote(value.username or "")
password = unquote(value.password or "")
database = unquote(value.path[1:] if value.path.startswith("/") else "")
port = value.port or 5432
if not username or not password or not database or "/" in database:
    raise SystemExit("PostgreSQL DATABASE_URL identity is incomplete")
if any(ord(character) < 32 for field in (username, database) for character in field):
    raise SystemExit("PostgreSQL DATABASE_URL identity is invalid")
identity = "\0".join((value.hostname, str(port), username, database)).encode()
print(value.hostname)
print(port)
print(username)
print(database)
print(hashlib.sha256(password.encode()).hexdigest())
print(hashlib.sha256(identity).hexdigest())
')
  [[ ${#POSTGRES_IDENTITY[@]} -eq 6 ]] \
    || die "PostgreSQL DATABASE_URL identity could not be resolved"
  DATABASE_HOST="${POSTGRES_IDENTITY[0]}"
  DATABASE_PORT="${POSTGRES_IDENTITY[1]}"
  DATABASE_USER="${POSTGRES_IDENTITY[2]}"
  DATABASE_NAME="${POSTGRES_IDENTITY[3]}"
  DATABASE_PASSWORD_DIGEST="${POSTGRES_IDENTITY[4]}"
  DATABASE_IDENTITY_DIGEST="${POSTGRES_IDENTITY[5]}"
  if [[ "${DATABASE_HOST}" == postgres ]]; then
    DATABASE_SOURCE="bundled-postgres"
  else
    DATABASE_SOURCE="external-postgres"
  fi
elif [[ "${DATABASE_URL}" == sqlite:* ]]; then
  DATABASE_SOURCE="sqlite"
  SQLITE_PATH="$(printf '%s' "${DATABASE_URL}" | python3 -c '
import sys
from urllib.parse import unquote, urlsplit
raw = sys.stdin.read()
value = urlsplit(raw)
if value.scheme != "sqlite" or value.netloc or value.query or value.fragment or not raw.startswith("sqlite:////"):
    raise SystemExit("SQLite DATABASE_URL must name one absolute local file")
path = unquote(raw[len("sqlite:///"):])
if not path.startswith("/") or path.endswith("/"):
    raise SystemExit("SQLite DATABASE_URL path is invalid")
print(path)
')" || die "SQLite DATABASE_URL is invalid"
  DATABASE_IDENTITY_DIGEST="$(sha256_text "sqlite:${SQLITE_PATH}")"
elif [[ -z "${DATABASE_URL}" ]]; then
  SQLITE_PATH="${TRINITY_DB_PATH:-/data/trinity.db}"
  [[ "${SQLITE_PATH}" == /* && "${SQLITE_PATH}" != */ ]] \
    || die "Default SQLite authority must be one absolute local file"
  DATABASE_URL="sqlite:///${SQLITE_PATH}"
  DATABASE_IDENTITY_DIGEST="$(sha256_text "sqlite:${SQLITE_PATH}")"
else
  die "Unsupported DATABASE_URL scheme; refusing to guess the authoritative database"
fi
echo "database_source=${DATABASE_SOURCE}" >> "${MANIFEST}"
echo "database_identity_sha256=sha256:${DATABASE_IDENTITY_DIGEST}" >> "${MANIFEST}"

if [[ "${DATABASE_SOURCE}" == bundled-postgres ]]; then
  [[ -n "${POSTGRES_CONTAINER}" ]] \
    || die "Backend names bundled postgres but no compose PostgreSQL container exists"
  container_running "${POSTGRES_CONTAINER}" \
    || die "PostgreSQL container ${POSTGRES_CONTAINER} is stopped; refusing an unverifiable dump"
  POSTGRES_ENV="$(docker inspect "${POSTGRES_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}')"
  POSTGRES_USER="$(printf '%s\n' "${POSTGRES_ENV}" | sed -n 's/^POSTGRES_USER=//p' | head -n 1)"
  POSTGRES_DB="$(printf '%s\n' "${POSTGRES_ENV}" | sed -n 's/^POSTGRES_DB=//p' | head -n 1)"
  POSTGRES_PASSWORD="$(printf '%s\n' "${POSTGRES_ENV}" | sed -n 's/^POSTGRES_PASSWORD=//p' | head -n 1)"
  POSTGRES_USER="${POSTGRES_USER:-postgres}"
  POSTGRES_DB="${POSTGRES_DB:-${POSTGRES_USER}}"
  [[ "${DATABASE_PORT}" == 5432 \
    && "${POSTGRES_USER}" == "${DATABASE_USER}" \
    && "${POSTGRES_DB}" == "${DATABASE_NAME}" \
    && -n "${POSTGRES_PASSWORD}" \
    && "$(sha256_text "${POSTGRES_PASSWORD}")" == "${DATABASE_PASSWORD_DIGEST}" ]] \
    || die "Bundled PostgreSQL identity does not match the authoritative DATABASE_URL"
  capture_postgres_inventory "${POSTGRES_CONTAINER}" "${POSTGRES_USER}" "${POSTGRES_DB}" \
    "${RUN_DIR}/postgres-source-catalog.txt"
  grep '^data:' "${RUN_DIR}/postgres-source-catalog.txt" \
    > "${RUN_DIR}/postgres-source-data.txt" || true
  log "Creating PostgreSQL custom-format dump from ${POSTGRES_CONTAINER}"
  docker exec "${POSTGRES_CONTAINER}" sh -ec \
    'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "${RUN_DIR}/postgres.dump"

  [[ -s "${RUN_DIR}/postgres.dump" ]] || die "PostgreSQL dump is missing or empty"
  verify_postgres_dump_restore "${RUN_DIR}/postgres.dump" "${RUN_DIR}/postgres-source-catalog.txt"
  log "Verified postgres.dump by restoring it into a fresh PostgreSQL instance"
  echo "postgres_dump_sha256=sha256:$(sha256_file "${RUN_DIR}/postgres.dump")" >> "${MANIFEST}"
  echo "postgres_catalog_sha256=sha256:$(sha256_file "${RUN_DIR}/postgres-source-catalog.txt")" >> "${MANIFEST}"
  echo "postgres_content_fingerprint_sha256=sha256:$(sha256_file "${RUN_DIR}/postgres-source-data.txt")" >> "${MANIFEST}"
  echo "postgres_dump_verified=yes" >> "${MANIFEST}"
  echo "postgres_restore_verified=yes" >> "${MANIFEST}"
elif [[ "${DATABASE_SOURCE}" == external-postgres ]]; then
  echo "external_postgres_detected=yes" >> "${MANIFEST}"
  validate_external_snapshot_receipt "${DATABASE_URL}"
  echo "external_postgres_snapshot_verified=yes" >> "${MANIFEST}"
  echo "external_postgres_provider_verified=yes" >> "${MANIFEST}"
  warn "External PostgreSQL snapshot is represented by a signed, fresh, provider-verified receipt"
else
  log "Backend declares SQLite at its authoritative mounted path; taking an online backup"
  BACKEND_IMAGE="$(docker inspect "${BACKEND_CONTAINER}" --format '{{.Image}}')"
  [[ -n "${BACKEND_IMAGE}" ]] || die "Could not resolve the backend image for SQLite backup"
  SQLITE_MOUNT=()
  while IFS= read -r field; do
    SQLITE_MOUNT+=("${field}")
  done < <(docker inspect "${BACKEND_CONTAINER}" --format '{{json .Mounts}}' \
    | python3 -c '
import hashlib, json, os, sys
path = os.path.normpath(sys.argv[1])
mounts = json.load(sys.stdin)
candidates = []
for mount in mounts:
    destination = os.path.normpath(mount.get("Destination", ""))
    if mount.get("Type") not in {"bind", "volume"} or not destination.startswith("/"):
        continue
    if path == destination or path.startswith(destination.rstrip("/") + "/"):
        candidates.append((len(destination), mount, destination))
if not candidates:
    raise SystemExit("SQLite database is not under a persistent backend mount")
_, mount, destination = max(candidates, key=lambda item: item[0])
identity = "\0".join((mount["Type"], destination, mount.get("Name") or mount.get("Source") or "")).encode()
print(destination)
print(hashlib.sha256(identity).hexdigest())
' "${SQLITE_PATH}")
  [[ ${#SQLITE_MOUNT[@]} -eq 2 ]] \
    || die "SQLite database mount authority could not be resolved"
  echo "sqlite_path=${SQLITE_PATH}" >> "${MANIFEST}"
  echo "sqlite_mount_destination=${SQLITE_MOUNT[0]}" >> "${MANIFEST}"
  echo "sqlite_mount_identity_sha256=sha256:${SQLITE_MOUNT[1]}" >> "${MANIFEST}"
  docker run --rm \
    --volumes-from "${BACKEND_CONTAINER}:ro" \
    -v "${RUN_DIR}:/backup" \
    --user root \
    --entrypoint python3 \
    "${BACKEND_IMAGE}" \
    -c 'import sqlite3,sys,urllib.parse; path=urllib.parse.quote(sys.argv[1], safe="/"); source=sqlite3.connect(f"file:{path}?mode=ro", uri=True); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()' \
    "${SQLITE_PATH}" /backup/trinity.db
  [[ -s "${RUN_DIR}/trinity.db" ]] || die "SQLite online backup is missing or empty"
  SQLITE_FINGERPRINT_PROGRAM='import hashlib,json,sqlite3,sys,urllib.parse
path=urllib.parse.quote(sys.argv[1], safe="/")
db=sqlite3.connect(f"file:{path}?mode=ro", uri=True)
if db.execute("PRAGMA integrity_check").fetchone() != ("ok",): raise SystemExit("integrity")
tables=db.execute("SELECT name,coalesce(sql,\"\") FROM sqlite_master WHERE type=\"table\" AND name NOT LIKE \"sqlite_%\" ORDER BY name").fetchall()
if not tables: raise SystemExit("empty catalog")
rows=[]
for name,sql in tables:
    quoted=name.replace("\"", "\"\"")
    rows.append([name,sql,db.execute(f"SELECT count(*) FROM \"{quoted}\"").fetchone()[0]])
db.close()
payload=json.dumps(rows,sort_keys=True,separators=(",",":" )).encode()
print(hashlib.sha256(payload).hexdigest(),len(rows))'
  read -r SQLITE_SOURCE_FINGERPRINT SQLITE_SOURCE_TABLES < <(docker run --rm \
    --volumes-from "${BACKEND_CONTAINER}:ro" --user root --entrypoint python3 \
    "${BACKEND_IMAGE}" -c "${SQLITE_FINGERPRINT_PROGRAM}" "${SQLITE_PATH}")
  read -r SQLITE_BACKUP_FINGERPRINT SQLITE_BACKUP_TABLES < <(docker run --rm \
    -v "${RUN_DIR}:/backup:ro" --user root --entrypoint python3 \
    "${BACKEND_IMAGE}" -c "${SQLITE_FINGERPRINT_PROGRAM}" /backup/trinity.db)
  [[ "${SQLITE_SOURCE_FINGERPRINT}" == "${SQLITE_BACKUP_FINGERPRINT}" \
    && "${SQLITE_SOURCE_TABLES}" == "${SQLITE_BACKUP_TABLES}" \
    && "${SQLITE_SOURCE_TABLES}" -gt 0 ]] \
    || die "SQLite backup schema/table counts differ from the paused source"
  docker run --rm \
    -v "${RUN_DIR}:/backup" \
    alpine:3.20 \
    tar -czf /backup/sqlite-data.tgz -C /backup trinity.db
  verify_archive "${RUN_DIR}" "sqlite-data.tgz"
  docker run --rm -v "${RUN_DIR}:/backup:ro" alpine:3.20 \
    tar -tzf /backup/sqlite-data.tgz trinity.db >/dev/null \
    || die "SQLite backup does not contain trinity.db"
  echo "sqlite_backup_sha256=sha256:$(sha256_file "${RUN_DIR}/trinity.db")" >> "${MANIFEST}"
  echo "sqlite_content_fingerprint_sha256=sha256:${SQLITE_BACKUP_FINGERPRINT}" >> "${MANIFEST}"
  echo "sqlite_application_tables=${SQLITE_BACKUP_TABLES}" >> "${MANIFEST}"
  echo "sqlite_backup_verified=yes" >> "${MANIFEST}"
fi

if [[ ${INCLUDE_BACKEND_DATA} -eq 1 ]]; then
  log "Archiving backend /data mount"
  docker run --rm \
    --volumes-from "${BACKEND_CONTAINER}:ro" \
    -v "${RUN_DIR}:/backup" \
    alpine:3.20 \
    sh -ec 'cd /data; tar -czf /backup/backend-data.tgz .'
  verify_archive "${RUN_DIR}" "backend-data.tgz"
  docker run --rm \
    --volumes-from "${BACKEND_CONTAINER}:ro" \
    -v "${RUN_DIR}:/backup:ro" \
    -v "${ARCHIVE_RESTORE_VERIFIER}:/verify-archive-restore.sh:ro" \
    --tmpfs /restore:rw,nosuid,nodev \
    alpine:3.20 \
    sh -ec '
      mkdir -p /restore/tree
      tar -xzf /backup/backend-data.tgz -C /restore/tree
      sh /verify-archive-restore.sh /data /restore/tree
    ' \
    || die "Backend data archive does not exactly restore the paused /data mount"
  echo "backend_data_verified=yes" >> "${MANIFEST}"
  echo "backend_data_live_consistency_verified=yes" >> "${MANIFEST}"
  echo "backend_data_sha256=sha256:$(sha256_file "${RUN_DIR}/backend-data.tgz")" >> "${MANIFEST}"
fi

if [[ -n "${POSTGRES_CONTAINER}" ]]; then
  pause_container "${POSTGRES_CONTAINER}"
fi
assert_writers_still_paused

PLATFORM_ARCHIVE_DIR="${RUN_DIR}/platform-volumes"
mkdir -p "${PLATFORM_ARCHIVE_DIR}"
platform_volume_count=0
for volume in ${COMPOSE_NAMED_VOLUME_INVENTORY[*]-}; do
  archive_volume "${volume}" "${PLATFORM_ARCHIVE_DIR}" "${volume}.tgz"
  platform_volume_count=$((platform_volume_count + 1))
done
[[ "$(compose_named_volume_inventory)" == "${COMPOSE_NAMED_VOLUME_INVENTORY}" ]] \
  || die "Compose named-volume inventory changed during backup"
: > "${RUN_DIR}/platform-volumes.sha256"
for archive in "${PLATFORM_ARCHIVE_DIR}"/*.tgz; do
  [[ -e "${archive}" ]] || continue
  printf '%s  %s\n' "$(sha256_file "${archive}")" "$(basename "${archive}")" \
    >> "${RUN_DIR}/platform-volumes.sha256"
done
[[ "$(wc -l < "${RUN_DIR}/platform-volumes.sha256" | tr -d ' ')" -eq ${platform_volume_count} ]] \
  || die "Compose named-volume digest inventory is incomplete"
echo "platform_volume_archives=${platform_volume_count}" >> "${MANIFEST}"
echo "platform_volume_inventory_sha256=sha256:$(sha256_text "${COMPOSE_NAMED_VOLUME_INVENTORY}")" >> "${MANIFEST}"
echo "platform_volume_digest_inventory_sha256=sha256:$(sha256_file "${RUN_DIR}/platform-volumes.sha256")" >> "${MANIFEST}"
echo "platform_volume_archives_verified=yes" >> "${MANIFEST}"
echo "platform_volume_inventory_stable=yes" >> "${MANIFEST}"

if [[ ${INCLUDE_AGENT_WORKSPACES} -eq 1 ]]; then
  log "Archiving agent workspace volumes"
  AGENT_ARCHIVE_DIR="${RUN_DIR}/agent-workspaces"
  agent_count=0
  for volume in ${AGENT_WORKSPACE_VOLUMES[*]-}; do
    agent_count=$((agent_count + 1))
    archive_volume "${volume}" "${AGENT_ARCHIVE_DIR}" "${volume}.tgz"
  done
  echo "agent_workspace_archives=${agent_count}" >> "${MANIFEST}"
  echo "agent_workspace_containers=${AGENT_CONTAINER_COUNT}" >> "${MANIFEST}"
  [[ ${agent_count} -eq ${AGENT_WORKSPACE_VOLUME_COUNT} ]] \
    || die "Agent workspace archive inventory changed during backup"
  [[ ${agent_count} -ge ${AGENT_CONTAINER_COUNT} ]] \
    || die "Agent workspace archives do not cover every live agent container"
  : > "${RUN_DIR}/agent-workspaces.sha256"
  for archive in "${AGENT_ARCHIVE_DIR}"/*.tgz; do
    [[ -e "${archive}" ]] || continue
    printf '%s  %s\n' "$(sha256_file "${archive}")" "$(basename "${archive}")" \
      >> "${RUN_DIR}/agent-workspaces.sha256"
  done
  [[ "$(wc -l < "${RUN_DIR}/agent-workspaces.sha256" | tr -d ' ')" -eq ${agent_count} ]] \
    || die "Agent workspace digest inventory is incomplete"
  echo "agent_workspace_digest_inventory_sha256=sha256:$(sha256_file "${RUN_DIR}/agent-workspaces.sha256")" >> "${MANIFEST}"
  echo "agent_workspace_archives_verified=yes" >> "${MANIFEST}"
  echo "agent_workspace_live_consistency_verified=yes" >> "${MANIFEST}"
  log "Archived ${agent_count} agent workspace volume(s)"
fi

if [[ -n "${REDIS_CONTAINER}" ]]; then
  echo "redis_container_present=yes" >> "${MANIFEST}"
fi

assert_agent_inventory_unchanged
assert_writers_still_paused
echo "agent_inventory_stable=yes" >> "${MANIFEST}"

ARTIFACT_INVENTORY="${RUN_DIR}/backup-artifacts.sha256"
: > "${ARTIFACT_INVENTORY}"
while IFS= read -r artifact; do
  relative="${artifact#${RUN_DIR}/}"
  printf '%s  %s\n' "$(sha256_file "${artifact}")" "${relative}" >> "${ARTIFACT_INVENTORY}"
done < <(find "${RUN_DIR}" -type f ! -name manifest.txt ! -name backup-artifacts.sha256 | sort)
[[ -s "${ARTIFACT_INVENTORY}" ]] || die "Backup artifact digest inventory is empty"
echo "artifact_inventory_sha256=sha256:$(sha256_file "${ARTIFACT_INVENTORY}")" >> "${MANIFEST}"
echo "artifact_inventory_verified=yes" >> "${MANIFEST}"

du -sh "${RUN_DIR}" | awk '{print "backup_size=" $1}' >> "${MANIFEST}"
BACKUP_COMPLETE=0
if [[ ${INCLUDE_AGENT_WORKSPACES} -eq 1 && ${INCLUDE_BACKEND_DATA} -eq 1 && ${INCLUDE_ENV} -eq 1 ]]; then
  echo "backup_complete=yes" >> "${MANIFEST}"
  BACKUP_COMPLETE=1
else
  echo "backup_complete=no" >> "${MANIFEST}"
  echo "backup_partial=yes" >> "${MANIFEST}"
fi

if [[ -n "${RESULT_FILE}" ]]; then
  mkdir -p "$(dirname "${RESULT_FILE}")"
  printf '%s\n' "${RUN_DIR}" > "${RESULT_FILE}"
  chmod 600 "${RESULT_FILE}"
fi

if [[ ${BACKUP_COMPLETE} -eq 1 ]]; then
  log "Backup complete"
else
  warn "Backup bundle is partial and is not release-eligible"
fi
log "Manifest: ${MANIFEST}"
log "Bundle size: $(du -sh "${RUN_DIR}" | awk '{print $1}')"
