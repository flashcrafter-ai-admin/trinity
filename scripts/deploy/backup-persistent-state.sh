#!/usr/bin/env bash
#
# Back up the persistent state of a running Trinity instance.
#
# This script is intentionally conservative:
#   - it discovers the running compose project by labels,
#   - it takes a logical PostgreSQL dump when a bundled postgres service exists,
#   - it archives backend /data and agent workspace volumes,
#   - it never stops or removes containers or volumes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-trinity}"
OUTPUT_DIR="${PROJECT_ROOT}/backups/persistent-state"
ENV_FILE="${PROJECT_ROOT}/.env"
INCLUDE_AGENT_WORKSPACES=1
INCLUDE_BACKEND_DATA=1
INCLUDE_ENV=1
EXTERNAL_DB_SNAPSHOT_RECEIPT=""
RESULT_FILE=""

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
}

require_cmd docker
docker info >/dev/null 2>&1 || die "Docker is not running"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${OUTPUT_DIR%/}/${TIMESTAMP}-${PROJECT_NAME}"
mkdir -p "${RUN_DIR}"
chmod 700 "${RUN_DIR}"

MANIFEST="${RUN_DIR}/manifest.txt"
BACKEND_CONTAINER="$(service_container backend || true)"
POSTGRES_CONTAINER="$(service_container postgres || true)"
REDIS_CONTAINER="$(service_container redis || true)"

{
  echo "trinity_persistent_state_backup=1"
  echo "timestamp_utc=${TIMESTAMP}"
  echo "compose_project=${PROJECT_NAME}"
  echo "git_head=$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "backup_host=$(hostname 2>/dev/null || echo unknown)"
  echo "backend_container=${BACKEND_CONTAINER:-missing}"
  echo "postgres_container=${POSTGRES_CONTAINER:-missing}"
  echo "redis_container=${REDIS_CONTAINER:-missing}"
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
    log "Copied ${ENV_FILE} to env.backup"
  else
    die "Env file is missing or empty at ${ENV_FILE}; refusing an unrecoverable credential backup"
  fi
fi

DATABASE_URL="$(docker inspect "${BACKEND_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^DATABASE_URL=//p' | head -n 1)"

if [[ -n "${POSTGRES_CONTAINER}" ]]; then
  container_running "${POSTGRES_CONTAINER}" \
    || die "PostgreSQL container ${POSTGRES_CONTAINER} is stopped; refusing an unverifiable dump"
  log "Creating PostgreSQL custom-format dump from ${POSTGRES_CONTAINER}"
  docker exec "${POSTGRES_CONTAINER}" sh -lc \
    'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; pg_dump -U "${POSTGRES_USER:-trinity}" -d "${POSTGRES_DB:-trinity}" -Fc' \
    > "${RUN_DIR}/postgres.dump"

  if docker run --rm -v "${RUN_DIR}:/backup:ro" postgres:16-alpine pg_restore -l /backup/postgres.dump >/dev/null 2>&1; then
    log "Verified postgres.dump with pg_restore -l"
    echo "postgres_dump_verified=yes" >> "${MANIFEST}"
  else
    echo "postgres_dump_verified=no" >> "${MANIFEST}"
    die "Could not verify postgres.dump with pg_restore"
  fi
elif [[ "${DATABASE_URL}" == postgresql://* || "${DATABASE_URL}" == postgres://* ]]; then
  echo "external_postgres_detected=yes" >> "${MANIFEST}"
  [[ -s "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" ]] \
    || die "Backend uses external PostgreSQL; provide --external-db-snapshot-receipt after completing a managed snapshot"
  require_cmd jq
  if command -v sha256sum >/dev/null 2>&1; then
    DATABASE_URL_DIGEST="sha256:$(printf '%s' "${DATABASE_URL}" | sha256sum | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    DATABASE_URL_DIGEST="sha256:$(printf '%s' "${DATABASE_URL}" | shasum -a 256 | awk '{print $1}')"
  else
    die "sha256sum or shasum is required to bind the external database snapshot receipt"
  fi
  jq -e --arg digest "${DATABASE_URL_DIGEST}" '
    (keys | sort) == ["createdAt","databaseUrlDigest","provider","schemaVersion","snapshotId"]
    and .schemaVersion == "trinity-external-database-snapshot/1"
    and .databaseUrlDigest == $digest
    and (.provider | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$"))
    and (.snapshotId | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$"))
    and (.createdAt | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z$"))
  ' "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" >/dev/null \
    || die "External PostgreSQL snapshot receipt is invalid or bound to another database"
  cp "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" "${RUN_DIR}/external-postgres-snapshot.json"
  chmod 600 "${RUN_DIR}/external-postgres-snapshot.json"
  echo "external_postgres_snapshot_verified=yes" >> "${MANIFEST}"
  warn "External PostgreSQL snapshot is represented by its bound managed-provider receipt"
else
  log "No PostgreSQL service detected; taking an online SQLite backup"
  BACKEND_IMAGE="$(docker inspect "${BACKEND_CONTAINER}" --format '{{.Image}}')"
  [[ -n "${BACKEND_IMAGE}" ]] || die "Could not resolve the backend image for SQLite backup"
  docker run --rm \
    --volumes-from "${BACKEND_CONTAINER}:ro" \
    -v "${RUN_DIR}:/backup" \
    --user root \
    --entrypoint python3 \
    "${BACKEND_IMAGE}" \
    -c 'import sqlite3; source=sqlite3.connect("file:/data/trinity.db?mode=ro", uri=True); target=sqlite3.connect("/backup/trinity.db"); source.backup(target); target.close(); source.close()'
  [[ -s "${RUN_DIR}/trinity.db" ]] || die "SQLite online backup is missing or empty"
  docker run --rm \
    -v "${RUN_DIR}:/backup:ro" \
    --user root \
    --entrypoint python3 \
    "${BACKEND_IMAGE}" \
    -c 'import sqlite3,sys; db=sqlite3.connect("file:/backup/trinity.db?mode=ro", uri=True); result=db.execute("PRAGMA integrity_check").fetchone(); db.close(); sys.exit(0 if result == ("ok",) else 1)' \
    || die "SQLite online backup failed PRAGMA integrity_check"
  docker run --rm \
    -v "${RUN_DIR}:/backup" \
    alpine:3.20 \
    tar -czf /backup/sqlite-data.tgz -C /backup trinity.db
  verify_archive "${RUN_DIR}" "sqlite-data.tgz"
  docker run --rm -v "${RUN_DIR}:/backup:ro" alpine:3.20 \
    tar -tzf /backup/sqlite-data.tgz trinity.db >/dev/null \
    || die "SQLite backup does not contain trinity.db"
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
  echo "backend_data_verified=yes" >> "${MANIFEST}"
fi

if [[ ${INCLUDE_AGENT_WORKSPACES} -eq 1 ]]; then
  log "Archiving agent workspace volumes"
  AGENT_ARCHIVE_DIR="${RUN_DIR}/agent-workspaces"
  agent_count=0
  while IFS= read -r volume; do
    [[ -n "${volume}" ]] || continue
    agent_count=$((agent_count + 1))
    archive_volume "${volume}" "${AGENT_ARCHIVE_DIR}" "${volume}.tgz"
  done < <(docker volume ls --format '{{.Name}}' | grep -E '^agent-.+-workspace$' | sort || true)
  echo "agent_workspace_archives=${agent_count}" >> "${MANIFEST}"
  log "Archived ${agent_count} agent workspace volume(s)"
fi

if [[ -n "${REDIS_CONTAINER}" ]]; then
  echo "redis_container_present=yes" >> "${MANIFEST}"
fi

du -sh "${RUN_DIR}" | awk '{print "backup_size=" $1}' >> "${MANIFEST}"
echo "backup_complete=yes" >> "${MANIFEST}"

if [[ -n "${RESULT_FILE}" ]]; then
  mkdir -p "$(dirname "${RESULT_FILE}")"
  printf '%s\n' "${RUN_DIR}" > "${RESULT_FILE}"
  chmod 600 "${RESULT_FILE}"
fi

log "Backup complete"
log "Manifest: ${MANIFEST}"
log "Bundle size: $(du -sh "${RUN_DIR}" | awk '{print $1}')"
