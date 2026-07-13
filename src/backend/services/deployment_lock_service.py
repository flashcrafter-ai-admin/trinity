"""Trinity-wide admission lease for governed agent fleet deployments."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from redis_breaker_util import get_breaker_redis
from deployment_admission import (
    LOCK_KEY,
    ORDINARY_RESERVATIONS_KEY,
    LEASE_RESERVATIONS_PREFIX,
    DeploymentLockUnavailable,
    DeploymentLockConflict,
    DeploymentLockRejected,
    MutationAdmissionAuthority,
    MutationReservation,
    assert_current_authority,
    current_authority_error,
)

IN_FLIGHT_KEY = ORDINARY_RESERVATIONS_KEY
LEASE_IN_FLIGHT_PREFIX = LEASE_RESERVATIONS_PREFIX
ENROLLED_KEY_PREFIX = "trinity:agent-deployment-enrolled:v2:"
LOCK_HEADER = "X-Trinity-Deployment-Lock"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600
_MUTATING_GET_PATHS = (
    re.compile(r"^/api/public/slack/oauth/callback$"),
    re.compile(r"^/api/files/[^/]+$"),
)


def _redis():
    client = get_breaker_redis()
    if client is None:
        raise DeploymentLockUnavailable("deployment lock authority is unavailable")
    return client


_AUTHORITY = MutationAdmissionAuthority(_redis)


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
        "phase",
    }
    if (
        not isinstance(state, dict)
        or set(state) != required
        or state.get("schemaVersion") != "trinity-agent-deployment-lock/1"
        or state.get("phase") not in {"draining", "active", "releasing"}
        or not isinstance(state.get("agentNames"), list)
    ):
        raise DeploymentLockUnavailable(
            "deployment lock authority contains invalid state"
        )
    return state


def active_lock() -> Dict[str, Any] | None:
    return _parse(_decode(_redis().get(LOCK_KEY)))


def mutation_requires_admission(method: str, path: str) -> bool:
    normalized_method = method.upper()
    if normalized_method == "GET":
        return any(pattern.fullmatch(path) for pattern in _MUTATING_GET_PATHS)
    if normalized_method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if normalized_method == "POST" and path == "/api/agents/deployment-lock":
        return False
    if normalized_method == "DELETE" and path == "/api/agents/deployment-lock":
        return False
    return True


def websocket_requires_admission(path: str) -> bool:
    return (
        path.startswith("/ws/voice/")
        or path.startswith("/api/voip/voice/")
        or (path.startswith("/api/agents/") and path.endswith("/terminal"))
        or path == "/api/system-agent/terminal"
    )


class DeploymentAdmissionMiddleware:
    """Pure-ASGI admission gate that owns the complete downstream lifetime."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        path = scope.get("path", "")
        required = (
            scope_type == "http"
            and mutation_requires_admission(scope.get("method", ""), path)
        ) or (scope_type == "websocket" and websocket_requires_admission(path))
        if not required:
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        try:
            reservation = begin_mutation(headers.get(LOCK_HEADER.lower()))
        except DeploymentLockRejected as exc:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1013, "reason": str(exc)})
            else:
                from starlette.responses import JSONResponse

                await JSONResponse(status_code=423, content={"detail": str(exc)})(
                    scope, receive, send
                )
            return
        except DeploymentLockUnavailable as exc:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1011, "reason": str(exc)})
            else:
                from starlette.responses import JSONResponse

                await JSONResponse(status_code=503, content={"detail": str(exc)})(
                    scope, receive, send
                )
            return

        await _AUTHORITY.run_reserved(reservation, self.app(scope, receive, send))


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
        "phase": "draining",
    }
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True)
    if not _redis().set(LOCK_KEY, raw, nx=True, ex=ttl_seconds):
        raise DeploymentLockConflict("another deployment owns the Trinity-wide lock")
    return state, token


def _enrolled_key(state: Dict[str, Any]) -> str:
    return f"{ENROLLED_KEY_PREFIX}{state['tokenDigest']}"


def enroll_candidate(token: str | None, agent_name: str) -> Dict[str, Any]:
    """Bind a proven-absent name to this lease before its create begins."""
    state = require_candidate(token, agent_name)
    client = _redis()
    key = _enrolled_key(state)
    client.sadd(key, agent_name)
    client.expire(key, int(state["ttlSeconds"]))
    return state


def require_enrolled_candidate(token: str | None, agent_name: str) -> Dict[str, Any]:
    state = require_candidate(token, agent_name)
    if not _redis().sismember(_enrolled_key(state), agent_name):
        raise DeploymentLockRejected("agent was not enrolled by the active deployment lock")
    return state


async def activate_lock_after_drain(token: str, timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Wait for pre-lease mutations to finish, then atomically activate the lease."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while _AUTHORITY.count(IN_FLIGHT_KEY) != 0:
        if asyncio.get_running_loop().time() >= deadline:
            try:
                _abandon_draining_lock(token)
            finally:
                raise DeploymentLockConflict("preexisting mutations did not drain before timeout")
        await asyncio.sleep(0.05)
    state = require_lock_token(token, allow_draining=True)
    active = {**state, "phase": "active"}
    client = _redis()
    current = _decode(client.get(LOCK_KEY))
    updated = json.dumps(active, separators=(",", ":"), sort_keys=True)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    script = """
-- trinity:reservation:activate
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[4])
if redis.call('get', KEYS[1]) == ARGV[1] and redis.call('zcard', KEYS[2]) == 0 then
  redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""
    if int(client.eval(script, 2, LOCK_KEY, IN_FLIGHT_KEY, current, updated, state["ttlSeconds"], now)) != 1:
        raise DeploymentLockConflict("deployment lock could not activate after drain")
    return active


def begin_mutation(token: str | None) -> MutationReservation:
    return _AUTHORITY.begin(token)


def end_mutation(reservation: MutationReservation) -> None:
    _AUTHORITY.end(reservation)


def adopt_mutation(reservation: MutationReservation) -> MutationReservation:
    return _AUTHORITY.adopt(reservation)


def mutation_admission_context(reservation: MutationReservation):
    return _AUTHORITY.context(reservation)


def _inherited_mutation_counter() -> MutationReservation | None:
    return _AUTHORITY.inherited()


def _reserve_governed_mutation(coro):
    return _AUTHORITY.reserve(coro)


def reserve_governed_mutation(coro):
    """Reserve now and return an async callable suitable for BackgroundTasks."""
    _, run = _reserve_governed_mutation(coro)
    return run


def reserve_governed_call(function, *args, **kwargs):
    """Reserve an async call before a response-owned background handoff."""
    return reserve_governed_mutation(function(*args, **kwargs))


def spawn_governed_mutation(coro, *, name: str | None = None) -> asyncio.Task:
    return _AUTHORITY.spawn(coro, name=name)


def spawn_mutation_when_admitted(
    function,
    *args,
    retry_seconds: float = 1.0,
    name: str | None = None,
    **kwargs,
) -> asyncio.Task:
    """Retry admission without holding a reservation, then run exactly once."""
    return _AUTHORITY.spawn_when_admitted(
        function,
        *args,
        retry_seconds=retry_seconds,
        name=name,
        **kwargs,
    )


def mutation_authority_error() -> BaseException | None:
    return current_authority_error()


def require_current_mutation_authority() -> None:
    assert_current_authority()


def governed_background_mutation(function):
    return _AUTHORITY.governed(function)


def require_lock_token(token: str | None, *, allow_draining: bool = False) -> Dict[str, Any]:
    state = active_lock()
    if state is None:
        raise DeploymentLockRejected("no active deployment lock")
    supplied = hashlib.sha256((token or "").encode()).hexdigest()
    if not hmac.compare_digest(supplied, str(state["tokenDigest"])):
        raise DeploymentLockRejected("active deployment lock token is required")
    if state["phase"] != "active" and not allow_draining:
        raise DeploymentLockRejected("deployment lock is still draining")
    return state


def require_mutation_admission(token: str | None) -> Dict[str, Any] | None:
    state = active_lock()
    if state is None:
        if token:
            raise DeploymentLockRejected("deployment lock is absent or expired")
        return None
    if state["phase"] != "active":
        raise DeploymentLockRejected("deployment lock is still draining")
    supplied = hashlib.sha256((token or "").encode()).hexdigest()
    if not hmac.compare_digest(supplied, str(state["tokenDigest"])):
        raise DeploymentLockRejected("active deployment lock token is required")
    return state


def require_candidate(token: str | None, agent_name: str) -> Dict[str, Any]:
    state = require_lock_token(token)
    if agent_name not in state["agentNames"]:
        raise DeploymentLockRejected("agent is outside the active deployment lock")
    return state


def _abandon_draining_lock(token: str) -> None:
    state = require_lock_token(token, allow_draining=True)
    if state["phase"] != "draining":
        raise DeploymentLockRejected("only a draining deployment lock can be abandoned")
    client = _redis()
    raw = _decode(client.get(LOCK_KEY))
    if raw is None:
        raise DeploymentLockRejected("deployment lock expired before release")
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "redis.call('del', KEYS[2]); return redis.call('del', KEYS[1]) else return 0 end"
    )
    if int(client.eval(script, 2, LOCK_KEY, _enrolled_key(state), raw)) != 1:
        raise DeploymentLockRejected("deployment lock changed before release")


async def release_lock_after_drain(
    token: str | None, timeout_seconds: float = 30.0
) -> Dict[str, Any]:
    """Stop new lease writes, drain admitted ones, then atomically remove the lease."""
    state = require_lock_token(token, allow_draining=True)
    supplied = hashlib.sha256((token or "").encode()).hexdigest()
    reservation_key = f"{LEASE_IN_FLIGHT_PREFIX}{supplied}"
    client = _redis()
    current = _decode(client.get(LOCK_KEY))
    if current is None:
        raise DeploymentLockRejected("deployment lock expired before release")
    if state["phase"] != "releasing":
        releasing = {**state, "phase": "releasing"}
        updated = json.dumps(releasing, separators=(",", ":"), sort_keys=True)
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1 else return 0 end"
        )
        if int(client.eval(script, 1, LOCK_KEY, current, updated, state["ttlSeconds"])) != 1:
            raise DeploymentLockRejected("deployment lock changed before release")
        state = releasing
        current = updated

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        count = _AUTHORITY.count(reservation_key)
        if count == 0:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise DeploymentLockConflict("lease-authorized mutations did not drain before timeout")
        await asyncio.sleep(0.05)

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    script = """
-- trinity:reservation:release
redis.call('zremrangebyscore', KEYS[3], '-inf', ARGV[2])
if redis.call('get', KEYS[1]) == ARGV[1] and redis.call('zcard', KEYS[3]) == 0 then
  redis.call('del', KEYS[2])
  redis.call('del', KEYS[3])
  return redis.call('del', KEYS[1])
end
return 0
"""
    if int(client.eval(script, 3, LOCK_KEY, _enrolled_key(state), reservation_key, current, now)) != 1:
        raise DeploymentLockRejected("deployment lock changed before drained release")
    return state
