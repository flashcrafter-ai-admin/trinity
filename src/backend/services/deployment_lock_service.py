"""Trinity-wide admission lease for governed agent fleet deployments."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from redis_breaker_util import get_breaker_redis

LOCK_KEY = "trinity:agent-deployment-lock:v1"
IN_FLIGHT_KEY = "trinity:agent-deployment-mutations:v1"
LEASE_IN_FLIGHT_PREFIX = "trinity:agent-deployment-lease-mutations:v1:"
ENROLLED_KEY_PREFIX = "trinity:agent-deployment-enrolled:v1:"
LOCK_HEADER = "X-Trinity-Deployment-Lock"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600
_CURRENT_MUTATION_COUNTER: ContextVar[tuple[str, asyncio.Task | None] | None] = ContextVar(
    "trinity_deployment_mutation_counter", default=None
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
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if method.upper() == "POST" and path == "/api/agents/deployment-lock":
        return False
    if method.upper() == "DELETE" and path == "/api/agents/deployment-lock":
        return False
    return True


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


def _counter_value() -> int:
    raw = _decode(_redis().get(IN_FLIGHT_KEY))
    try:
        return max(0, int(raw or "0"))
    except ValueError as exc:
        raise DeploymentLockUnavailable("deployment mutation counter contains invalid state") from exc


async def activate_lock_after_drain(token: str, timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Wait for pre-lease mutations to finish, then atomically activate the lease."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while _counter_value() != 0:
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
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] and "
        "tonumber(redis.call('get', KEYS[2]) or '0') == 0 then "
        "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1 else return 0 end"
    )
    if int(client.eval(script, 2, LOCK_KEY, IN_FLIGHT_KEY, current, updated, state["ttlSeconds"])) != 1:
        raise DeploymentLockConflict("deployment lock could not activate after drain")
    return active


def begin_mutation(token: str | None) -> str:
    """Atomically admit and count either an ordinary or lease-authorized mutation."""
    client = _redis()
    supplied = hashlib.sha256((token or "").encode()).hexdigest()
    lease_counter = f"{LEASE_IN_FLIGHT_PREFIX}{supplied}"
    script = (
        "local lock = redis.call('get', KEYS[1]); "
        "if not lock then "
        "if ARGV[1] ~= '' then return {3, ''} end; "
        "redis.call('incr', KEYS[2]); redis.call('expire', KEYS[2], 3600); return {1, KEYS[2]} end; "
        "local state = cjson.decode(lock); "
        "if state['phase'] ~= 'active' then return {2, lock} end; "
        "if state['tokenDigest'] ~= ARGV[2] then return {3, lock} end; "
        "redis.call('incr', KEYS[3]); redis.call('expire', KEYS[3], state['ttlSeconds']); "
        "return {1, KEYS[3]}"
    )
    result = client.eval(script, 3, LOCK_KEY, IN_FLIGHT_KEY, lease_counter, token or "", supplied)
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise DeploymentLockUnavailable("deployment mutation admission returned invalid state")
    code = int(result[0])
    if code == 1:
        counter_key = _decode(result[1])
        if not counter_key:
            raise DeploymentLockUnavailable("deployment mutation admission omitted its counter")
        return counter_key
    if code == 2:
        raise DeploymentLockRejected("deployment lock is not accepting mutations")
    raise DeploymentLockRejected("active deployment lock token is required")


def end_mutation(counter_key: str) -> None:
    script = (
        "local n = tonumber(redis.call('get', KEYS[1]) or '0'); "
        "if n <= 1 then redis.call('del', KEYS[1]); return 0 end; "
        "return redis.call('decr', KEYS[1])"
    )
    _redis().eval(script, 1, counter_key)


def adopt_mutation(counter_key: str) -> str:
    """Synchronously extend an already-admitted mutation for a detached child."""
    if counter_key != IN_FLIGHT_KEY and not counter_key.startswith(LEASE_IN_FLIGHT_PREFIX):
        raise DeploymentLockUnavailable("invalid inherited deployment mutation counter")
    client = _redis()
    script = (
        "local count = tonumber(redis.call('get', KEYS[2]) or '0'); "
        "if count <= 0 then return 0 end; "
        "local lock = redis.call('get', KEYS[1]); "
        "if KEYS[2] == ARGV[1] then "
        "if lock then local state = cjson.decode(lock); "
        "if state['phase'] ~= 'draining' then return 0 end end; "
        "else if not lock then return 0 end; local state = cjson.decode(lock); "
        "if state['tokenDigest'] ~= ARGV[2] or "
        "(state['phase'] ~= 'active' and state['phase'] ~= 'releasing') then return 0 end end; "
        "redis.call('incr', KEYS[2]); redis.call('expire', KEYS[2], 3600); return 1"
    )
    digest = (
        counter_key.removeprefix(LEASE_IN_FLIGHT_PREFIX)
        if counter_key.startswith(LEASE_IN_FLIGHT_PREFIX)
        else ""
    )
    if int(client.eval(script, 2, LOCK_KEY, counter_key, IN_FLIGHT_KEY, digest)) != 1:
        raise DeploymentLockRejected("inherited deployment mutation is no longer admitted")
    return counter_key


@contextmanager
def mutation_admission_context(counter_key: str):
    """Expose one admitted counter to awaited work and synchronously spawned children."""
    token = _CURRENT_MUTATION_COUNTER.set((counter_key, asyncio.current_task()))
    try:
        yield
    finally:
        _CURRENT_MUTATION_COUNTER.reset(token)


def _inherited_mutation_counter() -> str | None:
    admission = _CURRENT_MUTATION_COUNTER.get()
    if admission is None:
        return None
    counter_key, owner_task = admission
    return counter_key if owner_task is asyncio.current_task() else None


def _reserve_governed_mutation(coro):
    inherited = _inherited_mutation_counter()
    try:
        counter_key = adopt_mutation(inherited) if inherited else begin_mutation(None)
    except Exception:
        coro.close()
        raise

    async def run():
        try:
            with mutation_admission_context(counter_key):
                return await coro
        finally:
            end_mutation(counter_key)

    return counter_key, run


def reserve_governed_mutation(coro):
    """Reserve now and return an async callable suitable for BackgroundTasks."""
    _, run = _reserve_governed_mutation(coro)
    return run


def reserve_governed_call(function, *args, **kwargs):
    """Reserve an async call before a response-owned background handoff."""
    return reserve_governed_mutation(function(*args, **kwargs))


def spawn_governed_mutation(coro, *, name: str | None = None) -> asyncio.Task:
    """Reserve before detaching and hold admission through the entire child chain."""
    counter_key, run = _reserve_governed_mutation(coro)
    task_coro = run()

    try:
        return asyncio.create_task(task_coro, name=name)
    except Exception:
        task_coro.close()
        end_mutation(counter_key)
        coro.close()
        raise


def governed_background_mutation(function):
    """Count non-HTTP mutation jobs so lease acquisition drains them too."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        if _inherited_mutation_counter():
            return await function(*args, **kwargs)
        counted = begin_mutation(None)
        try:
            with mutation_admission_context(counted):
                return await function(*args, **kwargs)
        finally:
            end_mutation(counted)

    return wrapped


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
    counter_key = f"{LEASE_IN_FLIGHT_PREFIX}{supplied}"
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
        raw_count = _decode(client.get(counter_key))
        try:
            count = max(0, int(raw_count or "0"))
        except ValueError as exc:
            raise DeploymentLockUnavailable(
                "deployment lease mutation counter contains invalid state"
            ) from exc
        if count == 0:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise DeploymentLockConflict("lease-authorized mutations did not drain before timeout")
        await asyncio.sleep(0.05)

    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] and "
        "tonumber(redis.call('get', KEYS[3]) or '0') == 0 then "
        "redis.call('del', KEYS[2]); redis.call('del', KEYS[3]); "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    if int(client.eval(script, 3, LOCK_KEY, _enrolled_key(state), counter_key, current)) != 1:
        raise DeploymentLockRejected("deployment lock changed before drained release")
    return state
