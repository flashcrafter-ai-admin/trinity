"""Trinity-wide admission lease for governed agent fleet deployments."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from redis_breaker_util import get_breaker_redis

LOCK_KEY = "trinity:agent-deployment-lock:v1"
LOCK_HEADER = "X-Trinity-Deployment-Lock"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600
_PROTECTED_MUTATION_PREFIXES = (
    "/api/agents",
    "/api/systems",
    "/api/system-agent",
    "/api/ops",
    "/api/subscriptions/agents",
    "/api/admin/soft-deleted/agents",
    "/api/monitoring/cleanup-trigger",
)


class DeploymentLockUnavailable(RuntimeError):
    pass


class DeploymentLockConflict(RuntimeError):
    pass


class DeploymentLockRejected(RuntimeError):
    pass


def _redis():
    client = get_breaker_redis()
    if client is None:
        raise DeploymentLockUnavailable("deployment lock authority is unavailable")
    return client


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse(raw: str | None) -> Dict[str, Any] | None:
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DeploymentLockUnavailable(
            "deployment lock authority contains invalid state"
        ) from exc
    required = {
        "schemaVersion",
        "owner",
        "fleetLockDigest",
        "agentNames",
        "tokenDigest",
        "acquiredAt",
        "ttlSeconds",
    }
    if (
        not isinstance(state, dict)
        or set(state) != required
        or state.get("schemaVersion") != "trinity-agent-deployment-lock/1"
        or not isinstance(state.get("agentNames"), list)
    ):
        raise DeploymentLockUnavailable(
            "deployment lock authority contains invalid state"
        )
    return state


def active_lock() -> Dict[str, Any] | None:
    return _parse(_decode(_redis().get(LOCK_KEY)))


def mutation_requires_admission(method: str, path: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if method.upper() == "POST" and path == "/api/agents/deployment-lock":
        return False
    return path.startswith(_PROTECTED_MUTATION_PREFIXES)


def acquire_lock(
    *, owner: str, fleet_lock_digest: str, agent_names: Iterable[str], ttl_seconds: int
) -> tuple[Dict[str, Any], str]:
    names = sorted(set(agent_names))
    if (
        not names
        or len(names) > 100
        or ttl_seconds < MIN_TTL_SECONDS
        or ttl_seconds > MAX_TTL_SECONDS
    ):
        raise ValueError("invalid deployment lock scope")
    token = secrets.token_urlsafe(48)
    state: Dict[str, Any] = {
        "schemaVersion": "trinity-agent-deployment-lock/1",
        "owner": owner,
        "fleetLockDigest": fleet_lock_digest,
        "agentNames": names,
        "tokenDigest": hashlib.sha256(token.encode()).hexdigest(),
        "acquiredAt": datetime.now(timezone.utc).isoformat(),
        "ttlSeconds": ttl_seconds,
    }
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True)
    if not _redis().set(LOCK_KEY, raw, nx=True, ex=ttl_seconds):
        raise DeploymentLockConflict("another deployment owns the Trinity-wide lock")
    return state, token


def require_lock_token(token: str | None) -> Dict[str, Any]:
    state = active_lock()
    if state is None:
        raise DeploymentLockRejected("no active deployment lock")
    supplied = hashlib.sha256((token or "").encode()).hexdigest()
    if not hmac.compare_digest(supplied, str(state["tokenDigest"])):
        raise DeploymentLockRejected("active deployment lock token is required")
    return state


def require_mutation_admission(token: str | None) -> Dict[str, Any] | None:
    state = active_lock()
    if state is None:
        if token:
            raise DeploymentLockRejected("deployment lock is absent or expired")
        return None
    supplied = hashlib.sha256((token or "").encode()).hexdigest()
    if not hmac.compare_digest(supplied, str(state["tokenDigest"])):
        raise DeploymentLockRejected("active deployment lock token is required")
    return state


def require_candidate(token: str | None, agent_name: str) -> Dict[str, Any]:
    state = require_lock_token(token)
    if agent_name not in state["agentNames"]:
        raise DeploymentLockRejected("agent is outside the active deployment lock")
    return state


def release_lock(token: str | None) -> Dict[str, Any]:
    state = require_lock_token(token)
    client = _redis()
    raw = _decode(client.get(LOCK_KEY))
    if raw is None:
        raise DeploymentLockRejected("deployment lock expired before release")
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    if int(client.eval(script, 1, LOCK_KEY, raw)) != 1:
        raise DeploymentLockRejected("deployment lock changed before release")
    return state
