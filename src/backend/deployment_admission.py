"""Shared Redis admission authority for deployment-safe mutations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Callable

LOCK_KEY = "trinity:agent-deployment-lock:v2"
ORDINARY_RESERVATIONS_KEY = "trinity:agent-deployment-reservations:v2"
LEASE_RESERVATIONS_PREFIX = "trinity:agent-deployment-lease-reservations:v2:"
RESERVATION_TTL_SECONDS = 300
RESERVATION_HEARTBEAT_SECONDS = 30


class DeploymentLockUnavailable(RuntimeError):
    pass


class DeploymentLockConflict(RuntimeError):
    pass


class DeploymentLockRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class MutationReservation:
    key: str
    reservation_id: str


@dataclass
class MutationAdmissionContext:
    reservation: MutationReservation
    owner_task: asyncio.Task | None
    authority_error: BaseException | None = None


_CURRENT_RESERVATION: ContextVar[MutationAdmissionContext | None] = ContextVar(
    "trinity_deployment_mutation_reservation", default=None
)


def _decode(value) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _now_and_expiry() -> tuple[int, int]:
    now = int(time.time() * 1000)
    return now, now + RESERVATION_TTL_SECONDS * 1000


def _lease_key(token_digest: str) -> str:
    return f"{LEASE_RESERVATIONS_PREFIX}{token_digest}"


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _validate_reservation(reservation: MutationReservation) -> None:
    if not isinstance(reservation, MutationReservation) or (
        reservation.key != ORDINARY_RESERVATIONS_KEY
        and not reservation.key.startswith(LEASE_RESERVATIONS_PREFIX)
    ):
        raise DeploymentLockUnavailable("invalid deployment mutation reservation")


def _parse_lock(raw: str) -> dict:
    try:
        state = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DeploymentLockUnavailable(
            "deployment lock authority contains invalid state"
        ) from exc
    if not isinstance(state, dict) or state.get("phase") not in {
        "draining",
        "active",
        "releasing",
    } or not isinstance(state.get("tokenDigest"), str):
        raise DeploymentLockUnavailable(
            "deployment lock authority contains invalid state"
        )
    return state


def current_authority_error() -> BaseException | None:
    context = _CURRENT_RESERVATION.get()
    return context.authority_error if context is not None else None


def assert_current_authority() -> None:
    error = current_authority_error()
    if error is not None:
        raise error


class MutationAdmissionAuthority:
    """Unique renewable reservations over one shared Redis authority."""

    def __init__(self, redis_provider: Callable):
        self._redis_provider = redis_provider

    def _redis(self):
        client = self._redis_provider()
        if client is None:
            raise DeploymentLockUnavailable("deployment lock authority is unavailable")
        return client

    def begin(self, token: str | None) -> MutationReservation:
        client = self._redis()
        token_digest = hashlib.sha256((token or "").encode()).hexdigest()
        current_raw = _decode(client.get(LOCK_KEY))
        expected_lock = current_raw or ""
        if current_raw is not None:
            state = _parse_lock(current_raw)
            if state["phase"] != "active":
                raise DeploymentLockRejected("deployment lock is not accepting mutations")
            if not hmac.compare_digest(token_digest, state["tokenDigest"]):
                raise DeploymentLockRejected("active deployment lock token is required")
        elif token:
            raise DeploymentLockRejected("active deployment lock token is required")

        reservation_id = secrets.token_urlsafe(32)
        now, expiry = _now_and_expiry()
        lease_key = _lease_key(token_digest)
        script = """
-- trinity:reservation:begin
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[1])
local lock = redis.call('get', KEYS[1])
if not lock then
  if ARGV[4] ~= '' then return {3, ''} end
  redis.call('zadd', KEYS[2], ARGV[2], ARGV[3])
  return {1, KEYS[2]}
end
if lock ~= ARGV[4] then return {3, lock} end
local state = cjson.decode(lock)
if state['phase'] ~= 'active' then return {2, lock} end
redis.call('zremrangebyscore', KEYS[3], '-inf', ARGV[1])
redis.call('zadd', KEYS[3], ARGV[2], ARGV[3])
redis.call('expire', KEYS[1], state['ttlSeconds'])
return {1, KEYS[3]}
"""
        result = client.eval(
            script,
            3,
            LOCK_KEY,
            ORDINARY_RESERVATIONS_KEY,
            lease_key,
            now,
            expiry,
            reservation_id,
            expected_lock,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise DeploymentLockUnavailable("deployment mutation admission returned invalid state")
        code = int(result[0])
        if code == 1:
            key = _decode(result[1])
            if not key:
                raise DeploymentLockUnavailable("deployment mutation admission omitted its set")
            return MutationReservation(key, reservation_id)
        if code == 2:
            raise DeploymentLockRejected("deployment lock is not accepting mutations")
        raise DeploymentLockRejected("active deployment lock token is required")

    def adopt(self, parent: MutationReservation) -> MutationReservation:
        _validate_reservation(parent)
        client = self._redis()
        child = MutationReservation(parent.key, secrets.token_urlsafe(32))
        now, expiry = _now_and_expiry()
        digest = parent.key.removeprefix(LEASE_RESERVATIONS_PREFIX)
        current_raw = _decode(client.get(LOCK_KEY))
        expected_lock = current_raw or ""
        if parent.key == ORDINARY_RESERVATIONS_KEY:
            if current_raw is not None and _parse_lock(current_raw)["phase"] != "draining":
                raise DeploymentLockRejected(
                    "inherited deployment mutation is no longer admitted"
                )
        else:
            if current_raw is None:
                raise DeploymentLockRejected(
                    "inherited deployment mutation is no longer admitted"
                )
            state = _parse_lock(current_raw)
            if state["phase"] not in {"active", "releasing"} or not hmac.compare_digest(
                digest, state["tokenDigest"]
            ):
                raise DeploymentLockRejected(
                    "inherited deployment mutation is no longer admitted"
                )
        script = """
-- trinity:reservation:adopt
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[4])
if not redis.call('zscore', KEYS[2], ARGV[1]) then return 0 end
local lock = redis.call('get', KEYS[1])
if (not lock and ARGV[3] ~= '') or (lock and lock ~= ARGV[3]) then return 0 end
if KEYS[2] == ARGV[2] then
  if lock then
    local state = cjson.decode(lock)
    if state['phase'] ~= 'draining' then return 0 end
  end
else
  if not lock then return 0 end
  local state = cjson.decode(lock)
  if state['phase'] ~= 'active' and state['phase'] ~= 'releasing' then return 0 end
end
redis.call('zadd', KEYS[2], ARGV[5], ARGV[6])
return 1
"""
        admitted = client.eval(
            script,
            2,
            LOCK_KEY,
            parent.key,
            parent.reservation_id,
            ORDINARY_RESERVATIONS_KEY,
            expected_lock,
            now,
            expiry,
            child.reservation_id,
        )
        if int(admitted) != 1:
            raise DeploymentLockRejected("inherited deployment mutation is no longer admitted")
        return child

    def refresh(self, reservation: MutationReservation) -> None:
        _validate_reservation(reservation)
        client = self._redis()
        now, expiry = _now_and_expiry()
        digest = reservation.key.removeprefix(LEASE_RESERVATIONS_PREFIX)
        current_raw = _decode(client.get(LOCK_KEY))
        expected_lock = current_raw or ""
        if reservation.key == ORDINARY_RESERVATIONS_KEY:
            if current_raw is not None and _parse_lock(current_raw)["phase"] != "draining":
                raise DeploymentLockRejected("deployment mutation reservation was lost")
        else:
            if current_raw is None:
                raise DeploymentLockRejected("deployment mutation reservation was lost")
            state = _parse_lock(current_raw)
            if state["phase"] not in {"active", "releasing"} or not hmac.compare_digest(
                digest, state["tokenDigest"]
            ):
                raise DeploymentLockRejected("deployment mutation reservation was lost")
        script = """
-- trinity:reservation:refresh
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[4])
if not redis.call('zscore', KEYS[2], ARGV[1]) then return 0 end
local lock = redis.call('get', KEYS[1])
if (not lock and ARGV[3] ~= '') or (lock and lock ~= ARGV[3]) then return 0 end
if KEYS[2] == ARGV[2] then
  if lock then
    local state = cjson.decode(lock)
    if state['phase'] ~= 'draining' then return 0 end
  end
else
  if not lock then return 0 end
  local state = cjson.decode(lock)
  if state['phase'] ~= 'active' and state['phase'] ~= 'releasing' then return 0 end
  redis.call('expire', KEYS[1], state['ttlSeconds'])
end
redis.call('zadd', KEYS[2], ARGV[5], ARGV[1])
return 1
"""
        refreshed = client.eval(
            script,
            2,
            LOCK_KEY,
            reservation.key,
            reservation.reservation_id,
            ORDINARY_RESERVATIONS_KEY,
            expected_lock,
            now,
            expiry,
        )
        if int(refreshed) != 1:
            raise DeploymentLockRejected("deployment mutation reservation was lost")

    def end(self, reservation: MutationReservation) -> None:
        _validate_reservation(reservation)
        self._redis().zrem(reservation.key, reservation.reservation_id)

    def count(self, key: str) -> int:
        now, _ = _now_and_expiry()
        script = """
-- trinity:reservation:count
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
return redis.call('zcard', KEYS[1])
"""
        return int(self._redis().eval(script, 1, key, now))

    @contextmanager
    def context(self, reservation: MutationReservation):
        context = MutationAdmissionContext(reservation, _current_task())
        token = _CURRENT_RESERVATION.set(context)
        try:
            yield context
        finally:
            _CURRENT_RESERVATION.reset(token)

    @staticmethod
    def inherited(*, same_task_only: bool = False) -> MutationReservation | None:
        admission = _CURRENT_RESERVATION.get()
        if admission is None:
            return None
        if same_task_only and (
            admission.owner_task is None or admission.owner_task is not _current_task()
        ):
            return None
        return admission.reservation

    async def run_reserved(self, reservation: MutationReservation, coro):
        owner_task = asyncio.current_task()
        try:
            self.refresh(reservation)
        except BaseException:
            try:
                self.end(reservation)
            except BaseException:
                pass
            if hasattr(coro, "close"):
                coro.close()
            raise
        with self.context(reservation) as context:
            async def heartbeat() -> None:
                try:
                    while True:
                        await asyncio.sleep(RESERVATION_HEARTBEAT_SECONDS)
                        self.refresh(reservation)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    context.authority_error = exc
                    if owner_task is not None:
                        owner_task.cancel()

            heartbeat_task = asyncio.create_task(
                heartbeat(), name="deployment-admission-heartbeat"
            )
            try:
                try:
                    result = await coro
                except asyncio.CancelledError:
                    assert_current_authority()
                    raise
                assert_current_authority()
                return result
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                try:
                    self.end(reservation)
                finally:
                    if hasattr(coro, "close") and not getattr(coro, "cr_running", False):
                        coro.close()

    def reserve(self, coro, *, token: str | None = None):
        inherited = self.inherited()
        try:
            reservation = self.adopt(inherited) if inherited else self.begin(token)
        except Exception:
            coro.close()
            raise

        async def run():
            return await self.run_reserved(reservation, coro)

        return reservation, run

    def spawn(self, coro, *, token: str | None = None, name: str | None = None):
        reservation, run = self.reserve(coro, token=token)
        task_coro = run()
        try:
            return asyncio.create_task(task_coro, name=name)
        except Exception:
            task_coro.close()
            try:
                self.end(reservation)
            finally:
                coro.close()
            raise

    def run_sync(self, function, *args, **kwargs):
        reservation = self.begin(None)
        stopped = threading.Event()
        heartbeat_thread = None
        try:
            with self.context(reservation) as context:
                def heartbeat() -> None:
                    while not stopped.wait(RESERVATION_HEARTBEAT_SECONDS):
                        try:
                            self.refresh(reservation)
                        except BaseException as exc:
                            context.authority_error = exc
                            return

                heartbeat_thread = threading.Thread(
                    target=heartbeat,
                    name="deployment-admission-sync-heartbeat",
                    daemon=True,
                )
                heartbeat_thread.start()
                result = function(*args, **kwargs)
                assert_current_authority()
                return result
        finally:
            stopped.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(
                    timeout=max(1.0, RESERVATION_HEARTBEAT_SECONDS + 1.0)
                )
            self.end(reservation)

    async def run_when_admitted(
        self,
        function,
        *args,
        retry_seconds: float = 1.0,
        **kwargs,
    ):
        while True:
            try:
                reservation = self.begin(None)
            except (DeploymentLockRejected, DeploymentLockUnavailable):
                await asyncio.sleep(retry_seconds)
                continue
            started = False

            async def invoke():
                nonlocal started
                started = True
                return await function(*args, **kwargs)

            try:
                return await self.run_reserved(reservation, invoke())
            except (DeploymentLockRejected, DeploymentLockUnavailable):
                if started:
                    raise
                await asyncio.sleep(retry_seconds)

    def spawn_when_admitted(
        self,
        function,
        *args,
        retry_seconds: float = 1.0,
        name: str | None = None,
        **kwargs,
    ):
        return asyncio.create_task(
            self.run_when_admitted(
                function, *args, retry_seconds=retry_seconds, **kwargs
            ),
            name=name,
        )

    def governed(self, function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            inherited = self.inherited()
            if inherited and self.inherited(same_task_only=True):
                return await function(*args, **kwargs)
            coro = function(*args, **kwargs)
            try:
                reservation = self.adopt(inherited) if inherited else self.begin(None)
            except Exception:
                coro.close()
                raise
            return await self.run_reserved(reservation, coro)

        return wrapped
