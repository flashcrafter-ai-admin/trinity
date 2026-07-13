"""Shared Redis admission authority for deployment-safe mutations."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
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


_CURRENT_RESERVATION: ContextVar[
    tuple[MutationReservation, asyncio.Task | None] | None
] = ContextVar("trinity_deployment_mutation_reservation", default=None)


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
        token_digest = hashlib.sha256((token or "").encode()).hexdigest()
        reservation_id = secrets.token_urlsafe(32)
        now, expiry = _now_and_expiry()
        lease_key = _lease_key(token_digest)
        script = """
-- trinity:reservation:begin
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[3])
local lock = redis.call('get', KEYS[1])
if not lock then
  if ARGV[1] ~= '' then return {3, ''} end
  redis.call('zadd', KEYS[2], ARGV[4], ARGV[5])
  return {1, KEYS[2]}
end
local state = cjson.decode(lock)
if state['phase'] ~= 'active' then return {2, lock} end
if state['tokenDigest'] ~= ARGV[2] then return {3, lock} end
redis.call('zremrangebyscore', KEYS[3], '-inf', ARGV[3])
redis.call('zadd', KEYS[3], ARGV[4], ARGV[5])
redis.call('expire', KEYS[1], state['ttlSeconds'])
return {1, KEYS[3]}
"""
        result = self._redis().eval(
            script,
            3,
            LOCK_KEY,
            ORDINARY_RESERVATIONS_KEY,
            lease_key,
            token or "",
            token_digest,
            now,
            expiry,
            reservation_id,
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
        child = MutationReservation(parent.key, secrets.token_urlsafe(32))
        now, expiry = _now_and_expiry()
        digest = parent.key.removeprefix(LEASE_RESERVATIONS_PREFIX)
        script = """
-- trinity:reservation:adopt
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[4])
if not redis.call('zscore', KEYS[2], ARGV[1]) then return 0 end
local lock = redis.call('get', KEYS[1])
if KEYS[2] == ARGV[2] then
  if lock then
    local state = cjson.decode(lock)
    if state['phase'] ~= 'draining' then return 0 end
  end
else
  if not lock then return 0 end
  local state = cjson.decode(lock)
  if state['tokenDigest'] ~= ARGV[3] or
     (state['phase'] ~= 'active' and state['phase'] ~= 'releasing') then return 0 end
end
redis.call('zadd', KEYS[2], ARGV[5], ARGV[6])
return 1
"""
        admitted = self._redis().eval(
            script,
            2,
            LOCK_KEY,
            parent.key,
            parent.reservation_id,
            ORDINARY_RESERVATIONS_KEY,
            digest,
            now,
            expiry,
            child.reservation_id,
        )
        if int(admitted) != 1:
            raise DeploymentLockRejected("inherited deployment mutation is no longer admitted")
        return child

    def refresh(self, reservation: MutationReservation) -> None:
        _validate_reservation(reservation)
        now, expiry = _now_and_expiry()
        digest = reservation.key.removeprefix(LEASE_RESERVATIONS_PREFIX)
        script = """
-- trinity:reservation:refresh
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[4])
if not redis.call('zscore', KEYS[2], ARGV[1]) then return 0 end
local lock = redis.call('get', KEYS[1])
if KEYS[2] == ARGV[2] then
  if lock then
    local state = cjson.decode(lock)
    if state['phase'] ~= 'draining' then return 0 end
  end
else
  if not lock then return 0 end
  local state = cjson.decode(lock)
  if state['tokenDigest'] ~= ARGV[3] or
     (state['phase'] ~= 'active' and state['phase'] ~= 'releasing') then return 0 end
  redis.call('expire', KEYS[1], state['ttlSeconds'])
end
redis.call('zadd', KEYS[2], ARGV[5], ARGV[1])
return 1
"""
        refreshed = self._redis().eval(
            script,
            2,
            LOCK_KEY,
            reservation.key,
            reservation.reservation_id,
            ORDINARY_RESERVATIONS_KEY,
            digest,
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
        token = _CURRENT_RESERVATION.set((reservation, _current_task()))
        try:
            yield
        finally:
            _CURRENT_RESERVATION.reset(token)

    @staticmethod
    def inherited(*, same_task_only: bool = False) -> MutationReservation | None:
        admission = _CURRENT_RESERVATION.get()
        if admission is None:
            return None
        reservation, owner = admission
        if same_task_only and (owner is None or owner is not _current_task()):
            return None
        return reservation

    async def run_reserved(self, reservation: MutationReservation, coro):
        heartbeat_failed: BaseException | None = None
        owner_task = asyncio.current_task()

        async def heartbeat() -> None:
            nonlocal heartbeat_failed
            try:
                while True:
                    await asyncio.sleep(RESERVATION_HEARTBEAT_SECONDS)
                    self.refresh(reservation)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # fail the owner instead of hiding lost authority
                heartbeat_failed = exc
                if owner_task is not None:
                    owner_task.cancel()

        self.refresh(reservation)
        heartbeat_task = asyncio.create_task(heartbeat(), name="deployment-admission-heartbeat")
        try:
            with self.context(reservation):
                try:
                    result = await coro
                except asyncio.CancelledError:
                    if heartbeat_failed is not None:
                        raise heartbeat_failed
                    raise
            if heartbeat_failed is not None:
                raise heartbeat_failed
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
        try:
            with self.context(reservation):
                return function(*args, **kwargs)
        finally:
            self.end(reservation)

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
