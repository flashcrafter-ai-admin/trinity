#!/usr/bin/env bash

set -euo pipefail

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-trinity}"
TIMEOUT_SECONDS="${TRINITY_READINESS_TIMEOUT_SECONDS:-180}"
INTERVAL_SECONDS="${TRINITY_READINESS_INTERVAL_SECONDS:-2}"
SERVICES=()

usage() {
  cat <<'EOF'
Usage: scripts/deploy/verify-compose-readiness.sh [--project-name NAME] -- SERVICE [...]

Waits until every selected Compose service is healthy or passes its explicit
endpoint probe. One-shot logs-init must exit successfully.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      PROJECT_NAME="${2:?--project-name requires a value}"
      shift 2
      ;;
    --)
      shift
      SERVICES=("$@")
      break
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

[[ ${#SERVICES[@]} -gt 0 ]] || {
  echo "At least one service is required" >&2
  exit 2
}
[[ "${TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "TRINITY_READINESS_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
}
[[ "${INTERVAL_SECONDS}" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "TRINITY_READINESS_INTERVAL_SECONDS must be a nonnegative number" >&2
  exit 2
}

service_container() {
  docker ps -a \
    --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=$1" \
    --format '{{.Names}}' \
    | head -n 1
}

published_http_probe() {
  local container="$1" container_port="$2" path="$3" binding host_port
  binding="$(docker port "${container}" "${container_port}/tcp" 2>/dev/null | head -n 1)"
  [[ -n "${binding}" ]] || return 1
  host_port="${binding##*:}"
  [[ "${host_port}" =~ ^[0-9]+$ ]] || return 1
  curl --fail --silent --show-error --max-time 5 \
    "http://127.0.0.1:${host_port}${path}" >/dev/null
}

service_ready() {
  local service="$1" container status health exit_code
  container="$(service_container "${service}")"
  [[ -n "${container}" ]] || return 1
  status="$(docker inspect --format '{{.State.Status}}' "${container}")"
  if [[ "${service}" == logs-init ]]; then
    exit_code="$(docker inspect --format '{{.State.ExitCode}}' "${container}")"
    [[ "${status}" == exited && "${exit_code}" == 0 ]]
    return
  fi
  [[ "${status}" == running ]] || return 1
  health="$(docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \
    "${container}")"
  if [[ "${health}" != no-healthcheck ]]; then
    [[ "${health}" == healthy ]]
    return
  fi
  case "${service}" in
    backend) published_http_probe "${container}" 8000 /health ;;
    frontend)
      published_http_probe "${container}" 8080 / \
        || published_http_probe "${container}" 80 /
      ;;
    mcp-server) published_http_probe "${container}" 8080 /health ;;
    otel-collector) published_http_probe "${container}" 13133 / ;;
    *) return 1 ;;
  esac
}

deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
  pending=()
  for service in "${SERVICES[@]}"; do
    service_ready "${service}" || pending+=("${service}")
  done
  if [[ ${#pending[@]} -eq 0 ]]; then
    printf '[readiness] Ready services:'
    printf ' %s' "${SERVICES[@]}"
    printf '\n'
    exit 0
  fi
  (( SECONDS < deadline )) || break
  sleep "${INTERVAL_SECONDS}"
done

printf '[readiness] Timed out waiting for:' >&2
printf ' %s' "${pending[@]}" >&2
printf '\n' >&2
for service in "${pending[@]}"; do
  container="$(service_container "${service}")"
  if [[ -z "${container}" ]]; then
    printf '[readiness] %s: missing container\n' "${service}" >&2
  else
    docker inspect --format \
      '[readiness] {{.Name}} status={{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "${container}" >&2 || true
  fi
done
exit 1
