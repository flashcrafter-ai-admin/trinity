#!/usr/bin/env bash
#
# Safe upgrade wrapper for a running Trinity instance.
#
# The guardrails are the point:
#   - keep one stable compose project name,
#   - take a persistent-state backup before changes,
#   - rebuild/recreate only platform services,
#   - never run docker compose down -v or remove data volumes,
#   - verify the running services after the upgrade.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
# shellcheck source=github-actions-safe-deploy.sh
source "${SCRIPT_DIR}/github-actions-safe-deploy.sh"

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-trinity}"
BACKUP_DIR="${PROJECT_ROOT}/backups/persistent-state"
ALLOW_FRESH=0
BUILD=1
NO_CACHE=0
DRY_RUN=0
ENV_FILE=""
EXTERNAL_DB_SNAPSHOT_RECEIPT=""
EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY=""
EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND=""
EXPECTED_SOURCE_REVISION="${TRINITY_EXPECTED_SOURCE_REVISION:-}"
COMPOSE_FILES=()
TARGET_SERVICES=()
BUILD_COMPOSE_FILES=()
RUNTIME_COMPOSE_FILES=()
EXTERNAL_INPUT_PATHS=()
EXTERNAL_INPUT_DIGESTS=()
IMMUTABLE_RELEASE_ROOT=""
IMMUTABLE_BUILD_SOURCE_ROOT=""
IMMUTABLE_RELEASE_DIGEST=""
IMMUTABLE_SOURCE_DIGEST=""
ENV_FILE_SOURCE=""
ENV_FILE_SOURCE_DIGEST=""
DOCKER_COMMAND=""
COMPOSE_PROCESS_ENV=()
COMPOSE_STATE_ROOT=""
RUNTIME_PROJECT_ROOT=""
RUNTIME_DATA_PATH=""

usage() {
  cat <<'EOF'
Usage: scripts/deploy/safe-upgrade.sh [options] [-- service ...]

Options:
  --project-name NAME       Docker Compose project name (default: trinity)
  -f, --compose-file FILE   Compose file to use. Repeatable.
  --env-file FILE           Compose env file
  --external-db-snapshot-receipt FILE
                            Bound receipt for an already-completed managed PostgreSQL snapshot
  --external-db-snapshot-public-key FILE
                            Root-owned Ed25519 key authorizing the external receipt
  --external-db-snapshot-verify-command FILE
                            Root-owned provider verifier for the external receipt
  --backup-dir DIR          Persistent-state backup parent directory
  --allow-fresh             Allow no existing containers/volumes (first install)
  --no-build                Skip docker compose build
  --no-cache                Build platform services with --no-cache
  --dry-run                 Print planned commands without running them
  -h, --help                Show this help

By default the script targets the platform services that exist in the compose
config: redis, vector, logs-init, postgres, backend, scheduler, frontend,
mcp-server, and otel-collector. Agent containers are not removed; their
workspace volumes are backed up by the pre-upgrade backup step.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      PROJECT_NAME="${2:?--project-name requires a value}"
      shift 2
      ;;
    -f|--compose-file)
      COMPOSE_FILES+=("${2:?--compose-file requires a value}")
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?--env-file requires a value}"
      shift 2
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
    --backup-dir)
      BACKUP_DIR="${2:?--backup-dir requires a value}"
      shift 2
      ;;
    --allow-fresh)
      ALLOW_FRESH=1
      shift
      ;;
    --no-build)
      BUILD=0
      shift
      ;;
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        TARGET_SERVICES+=("$1")
        shift
      done
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[safe-upgrade] %s\n' "$*"
}

die() {
  printf '[safe-upgrade] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  if [[ ${DRY_RUN} -eq 1 ]]; then
    printf '[safe-upgrade] DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

compose_execute() {
  local working_directory="$1"
  shift
  env -i "${COMPOSE_PROCESS_ENV[@]}" "PWD=${working_directory}" \
    "${DOCKER_COMMAND}" compose "$@"
}

run_compose() {
  local working_directory="$1"
  shift
  run env -i "${COMPOSE_PROCESS_ENV[@]}" "PWD=${working_directory}" \
    "${DOCKER_COMMAND}" compose "$@"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

absolute_path() {
  python3 - "$1" "${PROJECT_ROOT}" <<'PY'
import os
import sys

value = sys.argv[1]
if not os.path.isabs(value):
    value = os.path.join(sys.argv[2], value)
print(os.path.realpath(value))
PY
}

tree_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1]).resolve(strict=True)
digest = hashlib.sha256()

def field(value):
    encoded = str(value).encode("utf-8", "surrogateescape")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)

def visit(path):
    metadata = path.lstat()
    relative = "." if path == root else path.relative_to(root).as_posix()
    field(relative)
    field(f"{stat.S_IFMT(metadata.st_mode):o}")
    field(f"{stat.S_IMODE(metadata.st_mode):o}")
    field(metadata.st_uid)
    field(metadata.st_gid)
    if stat.S_ISREG(metadata.st_mode):
        field(metadata.st_nlink)
        field(metadata.st_size)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    elif stat.S_ISLNK(metadata.st_mode):
        field(os.readlink(path))
    elif stat.S_ISDIR(metadata.st_mode):
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            visit(child)
    else:
        raise SystemExit(f"unsupported immutable release input: {relative}")

visit(root)
print(digest.hexdigest())
PY
}

cleanup_release_inputs() {
  if [[ -n "${IMMUTABLE_RELEASE_ROOT}" && -d "${IMMUTABLE_RELEASE_ROOT}" ]]; then
    find -P "${IMMUTABLE_RELEASE_ROOT}" -type d -exec chmod u+w {} + 2>/dev/null || true
    rm -rf "${IMMUTABLE_RELEASE_ROOT}"
  fi
  if [[ -n "${COMPOSE_STATE_ROOT}" && -d "${COMPOSE_STATE_ROOT}" ]]; then
    rm -rf "${COMPOSE_STATE_ROOT}"
  fi
}
trap cleanup_release_inputs EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

assert_external_inputs_unchanged() {
  local index
  for ((index = 0; index < ${#EXTERNAL_INPUT_PATHS[@]}; index++)); do
    [[ -f "${EXTERNAL_INPUT_PATHS[$index]}" ]] \
      || die "Governed external release input disappeared"
    [[ "$(sha256_file "${EXTERNAL_INPUT_PATHS[$index]}")" == "${EXTERNAL_INPUT_DIGESTS[$index]}" ]] \
      || die "Governed external release input changed during upgrade"
  done
  if [[ -n "${ENV_FILE_SOURCE}" ]]; then
    [[ -f "${ENV_FILE_SOURCE}" ]] || die "Governed environment source disappeared"
    [[ "$(sha256_file "${ENV_FILE_SOURCE}")" == "${ENV_FILE_SOURCE_DIGEST}" ]] \
      || die "Governed environment source changed during upgrade"
  fi
}

assert_immutable_release_inputs() {
  [[ -n "${IMMUTABLE_RELEASE_ROOT}" ]] || return 0
  [[ "$(tree_digest "${IMMUTABLE_RELEASE_ROOT}")" == "${IMMUTABLE_RELEASE_DIGEST}" ]] \
    || die "Immutable release inputs changed during upgrade"
  assert_external_inputs_unchanged
}

service_container() {
  docker ps \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=$1" \
    --format '{{.Names}}' \
    | head -n 1
}

assert_governed_source() {
  [[ -n "${EXPECTED_SOURCE_REVISION}" ]] || return 0
  [[ "${EXPECTED_SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
    || die "TRINITY_EXPECTED_SOURCE_REVISION must be an exact lowercase commit SHA"
  assert_exact_clean_worktree "${PROJECT_ROOT}" "${EXPECTED_SOURCE_REVISION}" \
    || die "Governed source identity changed during upgrade"
}

prepare_immutable_release_inputs() {
  [[ -n "${EXPECTED_SOURCE_REVISION}" ]] || return 0
  command -v python3 >/dev/null 2>&1 || die "python3 is required for immutable release inputs"
  command -v tar >/dev/null 2>&1 || die "tar is required for immutable release inputs"
  if git -C "${PROJECT_ROOT}" ls-tree -r "${EXPECTED_SOURCE_REVISION}" \
    | awk '$1 == "160000" { found=1 } END { exit(found ? 0 : 1) }'; then
    die "Governed release source contains unsupported Git submodules"
  fi

  IMMUTABLE_RELEASE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/trinity-release-inputs.XXXXXX")"
  local source_root="${IMMUTABLE_RELEASE_ROOT}/source"
  local config_root="${IMMUTABLE_RELEASE_ROOT}/config"
  local archive="${IMMUTABLE_RELEASE_ROOT}/source.tar"
  mkdir -p "${source_root}" "${config_root}"
  local -a release_tool_environment=(
    env -i
    "PATH=/usr/local/bin:/usr/bin:/bin"
    "HOME=${config_root}"
    "TMPDIR=/tmp"
    "LANG=C.UTF-8"
    "LC_ALL=C.UTF-8"
    "GIT_CONFIG_NOSYSTEM=1"
  )
  "${release_tool_environment[@]}" git -C "${PROJECT_ROOT}" archive --format=tar \
    --output="${archive}" "${EXPECTED_SOURCE_REVISION}"
  "${release_tool_environment[@]}" tar -xf "${archive}" -C "${source_root}"
  rm -f "${archive}"
  "${release_tool_environment[@]}" python3 \
    "${PROJECT_ROOT}/scripts/deploy/verify-exact-git-tree.py" \
    "${PROJECT_ROOT}" "${EXPECTED_SOURCE_REVISION}" "${source_root}"
  IMMUTABLE_BUILD_SOURCE_ROOT="${source_root}"

  BUILD_COMPOSE_FILES=()
  RUNTIME_COMPOSE_FILES=()
  local compose_file relative frozen input_digest index=0
  for compose_file in "${COMPOSE_FILES[@]}"; do
    if [[ "${compose_file}" == "${PROJECT_ROOT}/"* ]]; then
      relative="${compose_file#${PROJECT_ROOT}/}"
      frozen="${source_root}/${relative}"
      [[ -f "${frozen}" && ! -L "${frozen}" ]] \
        || die "Governed compose file is absent from the exact Git object: ${relative}"
      BUILD_COMPOSE_FILES+=("${frozen}")
      RUNTIME_COMPOSE_FILES+=("${frozen}")
    else
      [[ ${index} -gt 0 ]] \
        || die "The first governed compose file must come from the exact Git object"
      frozen="${config_root}/compose-${index}.yml"
      input_digest="$(sha256_file "${compose_file}")"
      cp "${compose_file}" "${frozen}"
      chmod 400 "${frozen}"
      [[ "$(sha256_file "${compose_file}")" == "${input_digest}" \
        && "$(sha256_file "${frozen}")" == "${input_digest}" ]] \
        || die "Governed external compose input changed while it was frozen"
      BUILD_COMPOSE_FILES+=("${frozen}")
      RUNTIME_COMPOSE_FILES+=("${frozen}")
      EXTERNAL_INPUT_PATHS+=("${compose_file}")
      EXTERNAL_INPUT_DIGESTS+=("${input_digest}")
    fi
    index=$((index + 1))
  done

  if [[ -n "${ENV_FILE}" ]]; then
    ENV_FILE_SOURCE="${ENV_FILE}"
    ENV_FILE_SOURCE_DIGEST="$(sha256_file "${ENV_FILE_SOURCE}")"
    ENV_FILE="${config_root}/runtime.env"
    cp "${ENV_FILE_SOURCE}" "${ENV_FILE}"
    chmod 400 "${ENV_FILE}"
    [[ "$(sha256_file "${ENV_FILE_SOURCE}")" == "${ENV_FILE_SOURCE_DIGEST}" \
      && "$(sha256_file "${ENV_FILE}")" == "${ENV_FILE_SOURCE_DIGEST}" ]] \
      || die "Governed environment input changed while it was frozen"
  fi

  find -P "${IMMUTABLE_RELEASE_ROOT}" ! -type l -exec chmod a-w {} +
  IMMUTABLE_SOURCE_DIGEST="$(tree_digest "${source_root}")"
  IMMUTABLE_RELEASE_DIGEST="$(tree_digest "${IMMUTABLE_RELEASE_ROOT}")"
  assert_immutable_release_inputs
  log "Prepared exact-commit build source ${EXPECTED_SOURCE_REVISION} (sha256:${IMMUTABLE_SOURCE_DIGEST})"
}

validate_compose_build_inputs() {
  local project_root="$1"
  shift
  compose_execute "${project_root}" "$@" config --format json \
    | python3 "${project_root}/scripts/deploy/validate-compose-build-inputs.py" \
      "${project_root}" \
    || die "Compose build inputs are not closed over the exact release source"
}

git -C "${PROJECT_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "Safe upgrade requires a governed Git worktree"
if [[ -z "${EXPECTED_SOURCE_REVISION}" ]]; then
  EXPECTED_SOURCE_REVISION="$(git -C "${PROJECT_ROOT}" rev-parse HEAD)"
fi
export GIT_COMMIT="${EXPECTED_SOURCE_REVISION}"
export GIT_COMMIT_SUBJECT="$(git -C "${PROJECT_ROOT}" log -1 --format=%s "${EXPECTED_SOURCE_REVISION}")"
export GIT_COMMIT_TIMESTAMP="$(git -C "${PROJECT_ROOT}" log -1 --format=%cI "${EXPECTED_SOURCE_REVISION}")"
export GIT_BRANCH="detached-${EXPECTED_SOURCE_REVISION:0:8}"
export BUILD_DATE="${BUILD_DATE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
DOCKER_COMMAND="$(command -v docker || true)"
[[ -n "${DOCKER_COMMAND}" ]] || die "docker is required"
COMPOSE_STATE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/trinity-compose-state.XXXXXX")"
mkdir -p "${COMPOSE_STATE_ROOT}/docker"
chmod 700 "${COMPOSE_STATE_ROOT}" "${COMPOSE_STATE_ROOT}/docker"
COMPOSE_PROCESS_ENV=(
  "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  "HOME=${COMPOSE_STATE_ROOT}"
  "DOCKER_CONFIG=${COMPOSE_STATE_ROOT}/docker"
  "TMPDIR=/tmp"
  "LANG=C.UTF-8"
  "GIT_COMMIT=${GIT_COMMIT:-}"
  "GIT_COMMIT_SUBJECT=${GIT_COMMIT_SUBJECT:-}"
  "GIT_COMMIT_TIMESTAMP=${GIT_COMMIT_TIMESTAMP:-}"
  "GIT_BRANCH=${GIT_BRANCH:-}"
  "BUILD_DATE=${BUILD_DATE}"
)
assert_governed_source
if [[ -n "${EXPECTED_SOURCE_REVISION}" && "${GIT_COMMIT}" != "${EXPECTED_SOURCE_REVISION}" ]]; then
  die "Build provenance does not match the governed source revision"
fi

if [[ ${#COMPOSE_FILES[@]} -eq 0 ]]; then
  if [[ -f "${PROJECT_ROOT}/docker-compose.prod.yml" ]]; then
    COMPOSE_FILES=("${PROJECT_ROOT}/docker-compose.prod.yml")
  else
    COMPOSE_FILES=("${PROJECT_ROOT}/docker-compose.yml")
  fi
fi

RESOLVED_COMPOSE_FILES=()
for compose_file in "${COMPOSE_FILES[@]}"; do
  compose_file="$(absolute_path "${compose_file}")"
  [[ -f "${compose_file}" ]] || die "Compose file does not exist: ${compose_file}"
  RESOLVED_COMPOSE_FILES+=("${compose_file}")
done
COMPOSE_FILES=("${RESOLVED_COMPOSE_FILES[@]}")
if [[ -n "${ENV_FILE}" ]]; then
  ENV_FILE="$(absolute_path "${ENV_FILE}")"
  [[ -f "${ENV_FILE}" ]] || die "Environment file does not exist: ${ENV_FILE}"
fi

prepare_immutable_release_inputs
RUNTIME_PROJECT_ROOT="${IMMUTABLE_BUILD_SOURCE_ROOT:-${PROJECT_ROOT}}"

# Derive host-data authority only from the frozen env snapshot that Compose
# receives. Reading the mutable source before freezing could split freshness
# detection from the runtime configuration if the source changed in between.
RUNTIME_DATA_PATH="${PROJECT_ROOT}/trinity-data"
if [[ -n "${ENV_FILE}" ]]; then
  configured_data_path="$(sed -n 's/^TRINITY_DATA_PATH=//p' "${ENV_FILE}" | tail -n 1)"
  if [[ -n "${configured_data_path}" ]]; then
    RUNTIME_DATA_PATH="$(absolute_path "${configured_data_path}")"
  fi
fi
COMPOSE_PROCESS_ENV+=("TRINITY_DATA_PATH=${RUNTIME_DATA_PATH}")

COMPOSE_ARGS=(-p "${PROJECT_NAME}")
COMPOSE_ARGS+=(--project-directory "${RUNTIME_PROJECT_ROOT}")
if [[ -n "${ENV_FILE}" ]]; then
  COMPOSE_ARGS+=(--env-file "${ENV_FILE}")
fi
for compose_file in "${RUNTIME_COMPOSE_FILES[@]}"; do
  COMPOSE_ARGS+=(-f "${compose_file}")
done

BUILD_COMPOSE_ARGS=(-p "${PROJECT_NAME}")
BUILD_COMPOSE_ARGS+=(--project-directory "${IMMUTABLE_BUILD_SOURCE_ROOT:-${PROJECT_ROOT}}")
if [[ -n "${ENV_FILE}" ]]; then
  BUILD_COMPOSE_ARGS+=(--env-file "${ENV_FILE}")
fi
for compose_file in "${BUILD_COMPOSE_FILES[@]}"; do
  BUILD_COMPOSE_ARGS+=(-f "${compose_file}")
done

validate_compose_build_inputs "${IMMUTABLE_BUILD_SOURCE_ROOT}" "${BUILD_COMPOSE_ARGS[@]}"
assert_immutable_release_inputs

docker info >/dev/null 2>&1 || die "Docker is not running"

existing_containers="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT_NAME}" --format '{{.Names}}' || true)"
existing_volumes="$(docker volume ls --format '{{.Name}}' | grep -E "^${PROJECT_NAME}_" || true)"
existing_agent_workspaces="$(docker volume ls --format '{{.Name}}' | grep -E '^agent-.+-workspace$' || true)"
existing_configured_data=""
if [[ -e "${RUNTIME_DATA_PATH}" ]]; then
  existing_configured_data="${RUNTIME_DATA_PATH}"
fi
FRESH_INSTALL=0
if [[ -z "${existing_containers}${existing_volumes}${existing_agent_workspaces}${existing_configured_data}" && ${ALLOW_FRESH} -eq 0 ]]; then
  die "No existing containers or volumes for compose project ${PROJECT_NAME}. Use --allow-fresh only for first install."
fi
if [[ -z "${existing_containers}${existing_volumes}${existing_agent_workspaces}${existing_configured_data}" ]]; then
  FRESH_INSTALL=1
fi

assert_immutable_release_inputs
COMPOSE_SERVICES="$(compose_execute "${RUNTIME_PROJECT_ROOT}" "${COMPOSE_ARGS[@]}" config --services)"
assert_immutable_release_inputs
assert_governed_source

service_exists() {
  printf '%s\n' "${COMPOSE_SERVICES}" | grep -qx "$1"
}

if [[ ${#TARGET_SERVICES[@]} -eq 0 ]]; then
  for candidate in redis vector logs-init postgres backend scheduler frontend mcp-server otel-collector; do
    if service_exists "${candidate}"; then
      TARGET_SERVICES+=("${candidate}")
    fi
  done
fi

if [[ ${#TARGET_SERVICES[@]} -eq 0 ]]; then
  die "No target services found in compose config"
fi

log "Compose project: ${PROJECT_NAME}"
log "Compose files: ${COMPOSE_FILES[*]}"
log "Target services: ${TARGET_SERVICES[*]}"

if [[ ${FRESH_INSTALL} -eq 0 ]]; then
  BACKUP_RESULT_FILE="$(mktemp)"
  BACKUP_IMPLEMENTATION="${SCRIPT_DIR}/backup-persistent-state.sh"
  if [[ -n "${IMMUTABLE_RELEASE_ROOT}" ]]; then
    BACKUP_IMPLEMENTATION="${IMMUTABLE_RELEASE_ROOT}/source/scripts/deploy/backup-persistent-state.sh"
  fi
  BACKUP_CMD=(
    "${BACKUP_IMPLEMENTATION}"
    --project-name "${PROJECT_NAME}"
    --output-dir "${BACKUP_DIR}"
    --result-file "${BACKUP_RESULT_FILE}"
  )
  if [[ -n "${ENV_FILE}" ]]; then
    BACKUP_CMD+=(--env-file "${ENV_FILE}")
  fi
  if [[ -n "${EXTERNAL_DB_SNAPSHOT_RECEIPT}" ]]; then
    BACKUP_CMD+=(--external-db-snapshot-receipt "${EXTERNAL_DB_SNAPSHOT_RECEIPT}")
  fi
  if [[ -n "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}" ]]; then
    BACKUP_CMD+=(--external-db-snapshot-public-key "${EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY}")
  fi
  if [[ -n "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}" ]]; then
    BACKUP_CMD+=(--external-db-snapshot-verify-command "${EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND}")
  fi
  run "${BACKUP_CMD[@]}"
  if [[ ${DRY_RUN} -eq 0 ]]; then
    [[ -s "${BACKUP_RESULT_FILE}" ]] || die "Backup did not return a completed bundle path"
    COMPLETED_BACKUP="$(cat "${BACKUP_RESULT_FILE}")"
    rm -f "${BACKUP_RESULT_FILE}"
    case "${COMPLETED_BACKUP}" in
      "${BACKUP_DIR%/}/"*) ;;
      *) die "Backup result escaped the configured backup directory" ;;
    esac
    BACKUP_MANIFEST="${COMPLETED_BACKUP}/manifest.txt"
    [[ -f "${BACKUP_MANIFEST}" ]] || die "Backup manifest is missing"
    grep -qx 'backup_complete=yes' "${BACKUP_MANIFEST}" \
      || die "Backup manifest is not complete"
    grep -qx 'backend_data_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not verify backend data"
    grep -qx 'backend_data_live_consistency_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not compare backend data with the paused live source"
    grep -qx 'agent_workspace_archives_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not verify agent workspace coverage"
    grep -qx 'agent_workspace_live_consistency_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not compare agent workspaces with paused live sources"
    grep -qx 'environment_semantics_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not validate required environment keys"
    grep -qx 'writer_pause_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not hold every application writer paused"
    grep -qx 'agent_inventory_stable=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not prove stable agent container/workspace coverage"
    grep -qx 'artifact_inventory_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not produce a verified artifact digest inventory"
    grep -qx 'platform_volume_archives_verified=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not verify every Compose named-volume archive"
    grep -qx 'platform_volume_inventory_stable=yes' "${BACKUP_MANIFEST}" \
      || die "Backup did not prove stable Compose named-volume coverage"
    grep -Eq '^database_identity_sha256=sha256:[0-9a-f]{64}$' "${BACKUP_MANIFEST}" \
      || die "Backup did not bind the authoritative database identity"
    if grep -qx 'database_source=bundled-postgres' "${BACKUP_MANIFEST}"; then
      grep -qx 'postgres_dump_verified=yes' "${BACKUP_MANIFEST}" \
        && grep -qx 'postgres_restore_verified=yes' "${BACKUP_MANIFEST}" \
        && grep -Eq '^postgres_content_fingerprint_sha256=sha256:[0-9a-f]{64}$' "${BACKUP_MANIFEST}" \
        || die "Backup did not prove a fresh PostgreSQL restore"
    elif grep -qx 'database_source=external-postgres' "${BACKUP_MANIFEST}"; then
      grep -qx 'external_postgres_snapshot_verified=yes' "${BACKUP_MANIFEST}" \
        && grep -qx 'external_postgres_provider_verified=yes' "${BACKUP_MANIFEST}" \
        || die "Backup did not prove a signed provider-verified external snapshot"
    elif ! grep -qx 'database_source=sqlite' "${BACKUP_MANIFEST}" \
      || ! grep -qx 'sqlite_backup_verified=yes' "${BACKUP_MANIFEST}"; then
      die "Backup did not verify an authoritative database artifact"
    fi
    [[ -s "${COMPLETED_BACKUP}/env.backup" ]] \
      || die "Backup did not preserve a nonempty environment file"
    log "Verified persistent-state backup: ${COMPLETED_BACKUP}"
  fi
elif [[ ${FRESH_INSTALL} -eq 1 ]]; then
  log "First install has no persistent state to back up"
fi

if [[ ${BUILD} -eq 1 ]]; then
  BUILD_SERVICES=()
  for candidate in backend frontend mcp-server scheduler; do
    if service_exists "${candidate}"; then
      BUILD_SERVICES+=("${candidate}")
    fi
  done

  if [[ ${#BUILD_SERVICES[@]} -gt 0 ]]; then
    assert_governed_source
    assert_immutable_release_inputs
    if [[ ${NO_CACHE} -eq 1 ]]; then
      run_compose "${IMMUTABLE_BUILD_SOURCE_ROOT:-${PROJECT_ROOT}}" \
        "${BUILD_COMPOSE_ARGS[@]}" build --no-cache "${BUILD_SERVICES[@]}"
    else
      run_compose "${IMMUTABLE_BUILD_SOURCE_ROOT:-${PROJECT_ROOT}}" \
        "${BUILD_COMPOSE_ARGS[@]}" build "${BUILD_SERVICES[@]}"
    fi
    assert_immutable_release_inputs
    assert_governed_source
  fi
fi

assert_governed_source
assert_immutable_release_inputs
run_compose "${RUNTIME_PROJECT_ROOT}" "${COMPOSE_ARGS[@]}" up --no-build -d "${TARGET_SERVICES[@]}"

if [[ ${DRY_RUN} -eq 1 ]]; then
  log "Dry run complete"
  exit 0
fi

READINESS_IMPLEMENTATION="${RUNTIME_PROJECT_ROOT}/scripts/deploy/verify-compose-readiness.sh"
[[ -x "${READINESS_IMPLEMENTATION}" ]] \
  || die "Exact release source has no executable Compose readiness verifier"
log "Waiting for every selected service to become ready"
env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/var/empty \
  LANG=C.UTF-8 \
  TRINITY_READINESS_TIMEOUT_SECONDS="${TRINITY_READINESS_TIMEOUT_SECONDS:-180}" \
  "${READINESS_IMPLEMENTATION}" --project-name "${PROJECT_NAME}" -- "${TARGET_SERVICES[@]}" \
  || die "Selected Compose services did not become ready"

backend_container="$(service_container backend || true)"
[[ -n "${backend_container}" ]] || die "Running backend container was not found"
backend_status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${backend_container}")"
log "Backend container health: ${backend_status}"

VERSION_RESULT="$(mktemp)"
docker exec -i "${backend_container}" python3 - >"${VERSION_RESULT}" <<'PY' \
  || die "Authenticated version endpoint verification failed"
import json
import os
import urllib.parse
import urllib.request

password = os.environ.get("ADMIN_PASSWORD", "")
username = os.environ.get("ADMIN_USERNAME", "admin")
if not password or not username:
    raise SystemExit("backend admin authentication is unavailable")
login = urllib.request.Request(
    "http://127.0.0.1:8000/token",
    data=urllib.parse.urlencode({"username": username, "password": password}).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
)
with urllib.request.urlopen(login, timeout=15) as response:
    token_payload = json.load(response)
token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("backend did not issue an authentication token")
version = urllib.request.Request(
    "http://127.0.0.1:8000/api/version",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(version, timeout=15) as response:
    payload = json.load(response)
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
PY
[[ -s "${VERSION_RESULT}" ]] || die "Version endpoint returned an empty response"
command -v jq >/dev/null 2>&1 || die "jq is required to verify the version endpoint"
jq -e --arg commit "${GIT_COMMIT}" --arg short "${GIT_COMMIT:0:8}" '
  .git_commit == $commit
  and .git_commit_short == $short
  and .git_commit != "unknown"
' "${VERSION_RESULT}" >/dev/null \
  || die "Version endpoint does not match the exact deployed Git revision"
log "Running version: $(tr -d '\n' <"${VERSION_RESULT}")"
rm -f "${VERSION_RESULT}"
assert_immutable_release_inputs
assert_governed_source

log "Safe upgrade complete"
