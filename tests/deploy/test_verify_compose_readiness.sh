#!/usr/bin/env bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="$ROOT/scripts/deploy/verify-compose-readiness.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat > "$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
joined=" $* "
case "${1:-}" in
  ps)
    service=$(printf '%s' "$joined" | sed -n 's/.*label=com.docker.compose.service=\([^ ]*\).*/\1/p')
    [[ -n "$service" ]] || exit 2
    [[ "$service" != "${FAKE_MISSING_SERVICE:-}" ]] || exit 0
    printf 'fixture-%s\n' "$service"
    ;;
  inspect)
    container=${!#}
    service=${container#fixture-}
    if [[ "$joined" == *"{{.State.Status}}"* ]]; then
      [[ "$service" == logs-init ]] && echo exited || echo running
    elif [[ "$joined" == *"{{.State.ExitCode}}"* ]]; then
      [[ "$service" == logs-init ]] && echo "${FAKE_LOGS_INIT_EXIT:-0}" || echo 0
    elif [[ "$joined" == *"if .State.Health"* && "$joined" != *"[readiness]"* ]]; then
      case "$service" in
        redis|scheduler|vector|postgres)
          [[ "$service" == "${FAKE_UNHEALTHY_SERVICE:-}" ]] && echo unhealthy || echo healthy
          ;;
        *) echo no-healthcheck ;;
      esac
    elif [[ "$joined" == *"[readiness]"* ]]; then
      printf '[readiness] /%s status=running exit=0 health=none\n' "$container"
    else
      exit 2
    fi
    ;;
  port)
    container=${2:?}
    service=${container#fixture-}
    case "$service" in
      backend) echo '127.0.0.1:18000' ;;
      frontend) echo '127.0.0.1:18080' ;;
      mcp-server) echo '127.0.0.1:18081' ;;
      otel-collector) echo '127.0.0.1:18133' ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 2 ;;
esac
EOF

cat > "$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
url=${!#}
case "${FAKE_CURL_FAIL_SERVICE:-}" in
  backend) [[ "$url" != *":18000/health" ]] ;;
  frontend) [[ "$url" != *":18080/" ]] ;;
  mcp-server) [[ "$url" != *":18081/health" ]] ;;
  otel-collector) [[ "$url" != *":18133/" ]] ;;
  *) exit 0 ;;
esac
EOF
chmod 755 "$TMP/bin/docker" "$TMP/bin/curl"

services=(
  redis vector logs-init postgres backend scheduler frontend mcp-server otel-collector
)
env PATH="$TMP/bin:$PATH" TRINITY_READINESS_TIMEOUT_SECONDS=1 \
  TRINITY_READINESS_INTERVAL_SECONDS=0 "$SCRIPT" --project-name trinity -- \
  "${services[@]}" >/dev/null

if env PATH="$TMP/bin:$PATH" FAKE_UNHEALTHY_SERVICE=redis \
  TRINITY_READINESS_TIMEOUT_SECONDS=1 TRINITY_READINESS_INTERVAL_SECONDS=0 \
  "$SCRIPT" --project-name trinity -- "${services[@]}" >/dev/null 2>&1; then
  echo 'unhealthy Redis was accepted as ready' >&2
  exit 1
fi

if env PATH="$TMP/bin:$PATH" FAKE_LOGS_INIT_EXIT=9 \
  TRINITY_READINESS_TIMEOUT_SECONDS=1 TRINITY_READINESS_INTERVAL_SECONDS=0 \
  "$SCRIPT" --project-name trinity -- "${services[@]}" >/dev/null 2>&1; then
  echo 'failed logs-init was accepted as ready' >&2
  exit 1
fi

if env PATH="$TMP/bin:$PATH" FAKE_CURL_FAIL_SERVICE=mcp-server \
  TRINITY_READINESS_TIMEOUT_SECONDS=1 TRINITY_READINESS_INTERVAL_SECONDS=0 \
  "$SCRIPT" --project-name trinity -- "${services[@]}" >/dev/null 2>&1; then
  echo 'unreachable MCP server was accepted as ready' >&2
  exit 1
fi

if env PATH="$TMP/bin:$PATH" FAKE_MISSING_SERVICE=scheduler \
  TRINITY_READINESS_TIMEOUT_SECONDS=1 TRINITY_READINESS_INTERVAL_SECONDS=0 \
  "$SCRIPT" --project-name trinity -- "${services[@]}" >/dev/null 2>&1; then
  echo 'missing scheduler was accepted as ready' >&2
  exit 1
fi

echo 'verify-compose-readiness: OK'
