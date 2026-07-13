"""Governed fleet deployment lease and rollback regression tests."""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import docker
import pytest
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.testclient import TestClient

from services import deployment_lock_service as locks
from services import deployment_rollback_service as rollback
from services import cleanup_service as cleanup_module
from services import slot_service as slot_module


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.runtime_keys = []
        self.sets = {}
        self.zsets = {}

    def get(self, key):
        if key in self.zsets:
            return str(len(self.zsets[key]))
        return self.values.get(key)

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.sets.pop(key, None)
            self.zsets.pop(key, None)
            if key in self.runtime_keys:
                self.runtime_keys.remove(key)

    def expire(self, _key, _seconds):
        return True

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def srem(self, key, value):
        values = self.sets.get(key, set())
        removed = int(value in values)
        values.discard(value)
        if not values:
            self.sets.pop(key, None)
        return removed

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update({str(k): float(v) for k, v in mapping.items()})
        return len(mapping)

    def zrem(self, key, *members):
        rows = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if str(member) in rows:
                del rows[str(member)]
                removed += 1
        if not rows:
            self.zsets.pop(key, None)
        return removed

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zscore(self, key, member):
        return self.zsets.get(key, {}).get(str(member))

    def zremrangebyscore(self, key, _minimum, maximum):
        rows = self.zsets.get(key, {})
        expired = [member for member, score in rows.items() if score <= float(maximum)]
        return self.zrem(key, *expired)

    def eval(self, script, key_count, *args):
        if "trinity:candidate-operation:refresh" in script:
            key, operation_id, _ttl = args
            if self.values.get(key) != operation_id:
                return 0
            return 1
        if "trinity:candidate-operation:release" in script:
            key, operation_id = args
            if self.values.get(key) != operation_id:
                return 0
            del self.values[key]
            return 1
        if "trinity:reservation:begin" in script:
            lock_key, ordinary_key, lease_key, now, expiry, reservation_id, expected_lock = args
            self.zremrangebyscore(ordinary_key, "-inf", now)
            raw = self.values.get(lock_key)
            if raw is None:
                if expected_lock:
                    return [3, ""]
                self.zadd(ordinary_key, {reservation_id: expiry})
                return [1, ordinary_key]
            if raw != expected_lock:
                return [3, raw]
            state = json.loads(raw)
            if state["phase"] != "active":
                return [2, raw]
            self.zremrangebyscore(lease_key, "-inf", now)
            self.zadd(lease_key, {reservation_id: expiry})
            return [1, lease_key]
        if "trinity:reservation:adopt" in script:
            lock_key, key, parent_id, ordinary_key, expected_lock, now, expiry, child_id = args
            self.zremrangebyscore(key, "-inf", now)
            if self.zscore(key, parent_id) is None:
                return 0
            raw = self.values.get(lock_key)
            if (raw or "") != expected_lock:
                return 0
            if key == ordinary_key:
                if raw is not None and json.loads(raw)["phase"] != "draining":
                    return 0
            else:
                if raw is None:
                    return 0
                state = json.loads(raw)
                if state["phase"] not in {"active", "releasing"}:
                    return 0
            self.zadd(key, {child_id: expiry})
            return 1
        if "trinity:reservation:refresh" in script:
            lock_key, key, reservation_id, ordinary_key, expected_lock, now, expiry = args
            self.zremrangebyscore(key, "-inf", now)
            if self.zscore(key, reservation_id) is None:
                return 0
            raw = self.values.get(lock_key)
            if (raw or "") != expected_lock:
                return 0
            if key == ordinary_key:
                if raw is not None and json.loads(raw)["phase"] != "draining":
                    return 0
            else:
                if raw is None:
                    return 0
                state = json.loads(raw)
                if state["phase"] not in {"active", "releasing"}:
                    return 0
            self.zadd(key, {reservation_id: expiry})
            return 1
        if "trinity:reservation:count" in script:
            key, now = args
            self.zremrangebyscore(key, "-inf", now)
            return self.zcard(key)
        if "trinity:reservation:activate" in script:
            lock_key, reservations_key, expected, updated, _ttl, now = args
            self.zremrangebyscore(reservations_key, "-inf", now)
            if self.values.get(lock_key) != expected or self.zcard(reservations_key) != 0:
                return 0
            self.values[lock_key] = updated
            return 1
        if "trinity:reservation:release" in script:
            lock_key, enrolled_key, reservations_key, expected, now = args
            self.zremrangebyscore(reservations_key, "-inf", now)
            if self.values.get(lock_key) != expected or self.zcard(reservations_key) != 0:
                return 0
            self.sets.pop(enrolled_key, None)
            self.zsets.pop(reservations_key, None)
            del self.values[lock_key]
            return 1
        if key_count == 2 and "count <= 0" in script:
            lock_key, counter_key, ordinary_key, digest = args
            count = int(self.values.get(counter_key, "0"))
            if count <= 0:
                return 0
            raw = self.values.get(lock_key)
            if counter_key == ordinary_key:
                if raw is not None and json.loads(raw)["phase"] != "draining":
                    return 0
            else:
                if raw is None:
                    return 0
                state = json.loads(raw)
                if state["tokenDigest"] != digest or state["phase"] not in {
                    "active",
                    "releasing",
                }:
                    return 0
            self.values[counter_key] = str(count + 1)
            return 1
        if key_count == 3 and "cjson.decode" in script:
            lock_key, counter_key, lease_counter, token, digest = args
            raw = self.values.get(lock_key)
            if raw is None:
                if token:
                    return [3, ""]
                self.values[counter_key] = str(int(self.values.get(counter_key, "0")) + 1)
                return [1, counter_key]
            state = json.loads(raw)
            if state["phase"] != "active":
                return [2, raw]
            if state["tokenDigest"] != digest:
                return [3, raw]
            self.values[lease_counter] = str(int(self.values.get(lease_counter, "0")) + 1)
            return [1, lease_counter]
        if key_count == 2 and "ARGV[3]" in script:
            lock_key, counter_key, expected, updated, _ttl = args
            if self.values.get(lock_key) != expected or int(self.values.get(counter_key, "0")) != 0:
                return 0
            self.values[lock_key] = updated
            return 1
        if key_count == 1 and "ARGV[2]" in script:
            lock_key, expected, updated, _ttl = args
            if self.values.get(lock_key) != expected:
                return 0
            self.values[lock_key] = updated
            return 1
        if key_count == 3 and "KEYS[3]" in script:
            lock_key, enrolled_key, counter_key, expected = args
            if self.values.get(lock_key) != expected or int(self.values.get(counter_key, "0")) != 0:
                return 0
            self.sets.pop(enrolled_key, None)
            self.values.pop(counter_key, None)
            del self.values[lock_key]
            return 1
        if key_count == 2 and "del', KEYS[2]" in script:
            lock_key, enrolled_key, expected = args
            if self.values.get(lock_key) != expected:
                return 0
            self.sets.pop(enrolled_key, None)
            del self.values[lock_key]
            return 1
        if key_count == 1 and "decr" in script:
            key = args[0]
            value = int(self.values.get(key, "0"))
            if value <= 1:
                self.values.pop(key, None)
                return 0
            self.values[key] = str(value - 1)
            return value - 1
        key, expected = args
        if self.values.get(key) != expected:
            return 0
        del self.values[key]
        return 1

    def scan_iter(self, match):
        import fnmatch

        return (key for key in self.runtime_keys if fnmatch.fnmatch(key, match))


@pytest.mark.asyncio
async def test_deployment_lock_is_exclusive_scoped_and_atomically_released(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    state, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web", "agent-seo"],
        ttl_seconds=900,
    )
    assert state["agentNames"] == ["agent-seo", "agent-web"]
    assert state["phase"] == "draining"
    state = await locks.activate_lock_after_drain(token)
    assert state["phase"] == "active"
    assert locks.require_mutation_admission(token)["fleetLockDigest"] == "a" * 64
    assert locks.require_candidate(token, "agent-web")["owner"] == "creator"
    with pytest.raises(locks.DeploymentLockRejected, match="not enrolled"):
        locks.require_enrolled_candidate(token, "agent-web")
    locks.enroll_candidate(token, "agent-web")
    assert locks.require_enrolled_candidate(token, "agent-web")["owner"] == "creator"
    with pytest.raises(locks.DeploymentLockRejected):
        locks.require_mutation_admission("wrong")
    with pytest.raises(locks.DeploymentLockRejected):
        locks.require_candidate(token, "agent-other")
    with pytest.raises(locks.DeploymentLockConflict):
        locks.acquire_lock(
            owner="other",
            fleet_lock_digest="b" * 64,
            agent_names=["agent-other"],
            ttl_seconds=900,
        )
    assert (await locks.release_lock_after_drain(token))["fleetLockDigest"] == "a" * 64
    assert redis.sets == {}
    assert locks.require_mutation_admission(None) is None
    with pytest.raises(locks.DeploymentLockRejected):
        locks.require_mutation_admission(token)


@pytest.mark.asyncio
async def test_lock_drains_preexisting_mutation_and_blocks_new_ones(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    counted = locks.begin_mutation(None)
    assert counted.key == locks.IN_FLIGHT_KEY
    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    with pytest.raises(locks.DeploymentLockRejected, match="not accepting"):
        locks.begin_mutation(None)
    locks.end_mutation(counted)
    state = await locks.activate_lock_after_drain(token)
    assert state["phase"] == "active"
    with pytest.raises(locks.DeploymentLockRejected, match="token"):
        locks.begin_mutation(None)
    lease_counter = locks.begin_mutation(token)
    assert lease_counter.key.startswith(locks.LEASE_IN_FLIGHT_PREFIX)
    locks.end_mutation(lease_counter)


@pytest.mark.asyncio
async def test_release_blocks_new_writes_and_drains_authorized_mutation(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    await locks.activate_lock_after_drain(token)
    lease_counter = locks.begin_mutation(token)
    release = asyncio.create_task(locks.release_lock_after_drain(token, timeout_seconds=1))
    await asyncio.sleep(0.06)
    assert release.done() is False
    assert locks.active_lock()["phase"] == "releasing"
    with pytest.raises(locks.DeploymentLockRejected, match="not accepting"):
        locks.begin_mutation(token)
    locks.end_mutation(lease_counter)
    assert (await release)["phase"] == "releasing"
    assert locks.active_lock() is None


@pytest.mark.asyncio
async def test_background_mutation_is_counted_and_authority_absence_cannot_hide_a_lease(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    @locks.governed_background_mutation
    async def job():
        assert redis.get(locks.IN_FLIGHT_KEY) == "1"
        return "done"

    assert await job() == "done"
    assert redis.get(locks.IN_FLIGHT_KEY) is None
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: None)
    with pytest.raises(locks.DeploymentLockUnavailable):
        await job()


@pytest.mark.asyncio
async def test_acquisition_waits_for_detached_ordinary_child(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    child_started = asyncio.Event()
    child_finish = asyncio.Event()

    async def child():
        child_started.set()
        await child_finish.wait()

    parent_counter = locks.begin_mutation(None)
    with locks.mutation_admission_context(parent_counter):
        task = locks.spawn_governed_mutation(child())
    locks.end_mutation(parent_counter)
    await child_started.wait()
    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    activation = asyncio.create_task(locks.activate_lock_after_drain(token, timeout_seconds=1))
    await asyncio.sleep(0.06)
    assert activation.done() is False
    child_finish.set()
    await task
    assert (await activation)["phase"] == "active"


@pytest.mark.asyncio
async def test_executor_effect_keeps_independent_reservation_after_outer_cancel(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    started = threading.Event()
    finish = threading.Event()

    def blocking_effect():
        started.set()
        assert finish.wait(timeout=3)
        return "done"

    parent = locks.begin_mutation(None)
    with locks.mutation_admission_context(parent):
        effect_task = asyncio.create_task(
            locks._AUTHORITY.run_in_executor(None, blocking_effect)
        )
        assert await asyncio.to_thread(started.wait, 1)
    locks.end_mutation(parent)
    assert locks._AUTHORITY.count(locks.IN_FLIGHT_KEY) == 1

    effect_task.cancel()
    await asyncio.sleep(0.05)
    assert effect_task.done() is False
    assert locks._AUTHORITY.count(locks.IN_FLIGHT_KEY) == 1
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await effect_task
    assert locks._AUTHORITY.count(locks.IN_FLIGHT_KEY) == 0


@pytest.mark.asyncio
async def test_raw_task_cannot_reuse_parent_admission_context(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    observed = asyncio.Event()
    finish = asyncio.Event()

    @locks.governed_background_mutation
    async def child():
        assert redis.get(locks.IN_FLIGHT_KEY) == "2"
        observed.set()
        await finish.wait()

    parent_counter = locks.begin_mutation(None)
    with locks.mutation_admission_context(parent_counter):
        task = asyncio.create_task(child())
        await observed.wait()
    locks.end_mutation(parent_counter)
    assert redis.get(locks.IN_FLIGHT_KEY) == "1"
    finish.set()
    await task
    assert redis.get(locks.IN_FLIGHT_KEY) is None


@pytest.mark.asyncio
async def test_release_timeout_is_retryable_and_nested_child_extends_releasing_lease(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    parent_continue = asyncio.Event()
    grandchild_finish = asyncio.Event()
    grandchild_started = asyncio.Event()

    async def grandchild():
        grandchild_started.set()
        await grandchild_finish.wait()

    async def child():
        await parent_continue.wait()
        nested = locks.spawn_governed_mutation(grandchild())
        await nested

    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    await locks.activate_lock_after_drain(token)
    parent_counter = locks.begin_mutation(token)
    with locks.mutation_admission_context(parent_counter):
        child_task = locks.spawn_governed_mutation(child())
    locks.end_mutation(parent_counter)

    with pytest.raises(locks.DeploymentLockConflict, match="did not drain"):
        await locks.release_lock_after_drain(token, timeout_seconds=0.01)
    assert locks.active_lock()["phase"] == "releasing"
    parent_continue.set()
    await grandchild_started.wait()
    retry = asyncio.create_task(locks.release_lock_after_drain(token, timeout_seconds=1))
    await asyncio.sleep(0.06)
    assert retry.done() is False
    grandchild_finish.set()
    await child_task
    assert (await retry)["phase"] == "releasing"
    assert locks.active_lock() is None


@pytest.mark.asyncio
async def test_authority_loss_prevents_detached_child_from_starting(monkeypatch):
    started = False

    async def child():
        nonlocal started
        started = True

    monkeypatch.setattr(locks, "get_breaker_redis", lambda: None)
    with pytest.raises(locks.DeploymentLockUnavailable):
        locks.spawn_governed_mutation(child())
    await asyncio.sleep(0)
    assert started is False


@pytest.mark.asyncio
async def test_slot_release_callback_is_adopted_before_detach(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    callback_started = asyncio.Event()
    callback_finish = asyncio.Event()
    callback_done = asyncio.Event()

    class SlotRedis:
        def zrem(self, *_args):
            return 1

        def delete(self, *_args):
            return 1

        def zcard(self, *_args):
            return 0

    async def callback(_agent_name):
        callback_started.set()
        await callback_finish.wait()
        callback_done.set()

    service = slot_module.SlotService.__new__(slot_module.SlotService)
    service.redis = SlotRedis()
    service.slots_prefix = "agent:slots:"
    service.metadata_prefix = "agent:slot:"
    service._on_release_callbacks = [callback]
    parent_counter = locks.begin_mutation(None)
    with locks.mutation_admission_context(parent_counter):
        await service.release_slot("agent-web", "execution-1")
    await callback_started.wait()
    assert redis.get(locks.IN_FLIGHT_KEY) == "2"
    locks.end_mutation(parent_counter)
    assert redis.get(locks.IN_FLIGHT_KEY) == "1"
    callback_finish.set()
    await callback_done.wait()
    for _ in range(10):
        if redis.get(locks.IN_FLIGHT_KEY) is None:
            break
        await asyncio.sleep(0)
    assert redis.get(locks.IN_FLIGHT_KEY) is None


@pytest.mark.asyncio
async def test_cleanup_startup_write_and_first_sweep_share_one_admission(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    observed = []
    monkeypatch.setattr(
        cleanup_module.db,
        "mark_orphan_loops_interrupted",
        lambda: observed.append(redis.get(locks.IN_FLIGHT_KEY)) or 0,
    )
    service = cleanup_module.CleanupService(poll_interval=1)

    async def first_sweep():
        observed.append(redis.get(locks.IN_FLIGHT_KEY))
        return cleanup_module.CleanupReport()

    monkeypatch.setattr(service, "run_cleanup", first_sweep)
    await service._run_startup_cleanup_governed()
    assert observed == ["1", "1"]
    assert redis.get(locks.IN_FLIGHT_KEY) is None


def test_deployment_lock_fails_closed_without_authority(monkeypatch):
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: None)
    with pytest.raises(locks.DeploymentLockUnavailable):
        locks.require_mutation_admission(None)


@pytest.mark.parametrize(
    "path",
    [
        "/api/agents",
        "/api/agents/agent-web/credentials/inject",
        "/api/systems/system-a",
        "/api/system-agent/reinitialize",
        "/api/ops/emergency-stop",
        "/api/subscriptions/agents/agent-web",
        "/api/admin/soft-deleted/agents/agent-web/recover",
        "/api/monitoring/cleanup-trigger",
        "/api/mcp",
        "/internal/execute-task",
        "/webhooks/provider",
    ],
)
def test_all_agent_affecting_mutation_surfaces_require_admission(path):
    assert locks.mutation_requires_admission("POST", path) is True
    assert locks.mutation_requires_admission("GET", path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/public/slack/oauth/callback",
        "/api/files/file-1",
        "/api/public/intro/invitation-token",
        "/api/agents/agent-web/compatibility",
    ],
)
def test_write_bearing_get_surfaces_require_admission(path):
    assert locks.mutation_requires_admission("GET", path) is True


def test_redis_command_failures_are_normalized(monkeypatch):
    class BrokenRedis:
        def get(self, _key):
            raise ConnectionError("redis command failed")

    monkeypatch.setattr(locks, "get_breaker_redis", lambda: BrokenRedis())

    with pytest.raises(
        locks.DeploymentLockUnavailable,
        match="authority command failed",
    ):
        locks.active_lock()


@pytest.mark.asyncio
async def test_candidate_operation_serializes_rollback_and_prevents_resurrection(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    await locks.activate_lock_after_drain(token)
    locks.enroll_candidate(token, "agent-web")
    create_started = asyncio.Event()
    allow_create = asyncio.Event()
    rollback_entered = asyncio.Event()
    agent_exists = False

    async def create():
        nonlocal agent_exists
        async with locks.candidate_operation(token, "agent-web"):
            locks.require_enrolled_candidate(token, "agent-web")
            create_started.set()
            await allow_create.wait()
            agent_exists = True

    async def rollback_candidate():
        nonlocal agent_exists
        async with locks.candidate_operation(token, "agent-web"):
            rollback_entered.set()
            assert agent_exists is True
            agent_exists = False
            locks.unenroll_candidate(token, "agent-web")

    create_task = asyncio.create_task(create())
    await create_started.wait()
    rollback_task = asyncio.create_task(rollback_candidate())
    await asyncio.sleep(0.05)
    assert rollback_entered.is_set() is False
    allow_create.set()
    await create_task
    await rollback_task

    assert agent_exists is False
    with pytest.raises(locks.DeploymentLockRejected, match="not enrolled"):
        locks.require_enrolled_candidate(token, "agent-web")


@pytest.mark.asyncio
async def test_candidate_operation_fails_when_release_ownership_is_lost(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    await locks.activate_lock_after_drain(token)
    locks.enroll_candidate(token, "agent-web")

    with pytest.raises(
        locks.DeploymentLockRejected,
        match="serialization was lost",
    ):
        async with locks.candidate_operation(token, "agent-web"):
            operation_key = next(
                key
                for key in redis.values
                if key.startswith(locks.CANDIDATE_OPERATION_KEY_PREFIX)
            )
            redis.values[operation_key] = "different-owner"


@pytest.mark.asyncio
async def test_candidate_routes_serialize_create_then_rollback(monkeypatch):
    from models import AgentConfig
    from routers import agents as agents_router

    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    _, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    await locks.activate_lock_after_drain(token)
    locks.enroll_candidate(token, "agent-web")

    request = MagicMock()
    request.headers = {locks.LOCK_HEADER: token}
    request.client.host = "127.0.0.1"
    request.url.path = "/api/agents"
    user = MagicMock(username="creator", role="creator")
    create_started = asyncio.Event()
    allow_create = asyncio.Event()
    rollback_started = asyncio.Event()
    agent_exists = False

    def get_container(_agent_name):
        return MagicMock() if agent_exists else None

    async def create_agent(*_args, **_kwargs):
        nonlocal agent_exists
        create_started.set()
        await allow_create.wait()
        agent_exists = True
        return {"name": "agent-web"}

    async def rollback_agent(_agent_name):
        nonlocal agent_exists
        rollback_started.set()
        assert agent_exists is True
        agent_exists = False
        return {"complete": True, "proof": {}, "agentName": "agent-web"}

    monkeypatch.setattr(agents_router, "get_agent_container", get_container)
    monkeypatch.setattr(agents_router.db, "is_agent_name_reserved", lambda _name: False)
    monkeypatch.setattr(agents_router.db, "get_agent_owner", lambda _name: None)
    monkeypatch.setattr(agents_router, "create_agent_internal", create_agent)
    monkeypatch.setattr(agents_router, "rollback_candidate", rollback_agent)
    monkeypatch.setattr(agents_router.platform_audit_service, "log", AsyncMock())

    create_task = asyncio.create_task(
        agents_router.create_agent_endpoint(
            AgentConfig(name="agent-web"),
            request,
            user,
        )
    )
    await create_started.wait()
    rollback_task = asyncio.create_task(
        agents_router.rollback_deployment_candidate_endpoint(
            "agent-web",
            request,
            user,
        )
    )
    await asyncio.sleep(0.05)
    assert rollback_started.is_set() is False
    allow_create.set()
    await create_task
    await rollback_task

    assert agent_exists is False
    with pytest.raises(locks.DeploymentLockRejected, match="not enrolled"):
        locks.require_enrolled_candidate(token, "agent-web")


def test_read_only_get_surfaces_remain_available():
    assert locks.mutation_requires_admission("GET", "/api/agents") is False
    assert locks.mutation_requires_admission("GET", "/api/agents/agent-web/whatsapp") is False


def test_lock_lifecycle_endpoints_are_the_only_special_admission_paths():
    assert (
        locks.mutation_requires_admission("POST", "/api/agents/deployment-lock")
        is False
    )
    assert (
        locks.mutation_requires_admission("DELETE", "/api/agents/deployment-lock")
        is False
    )


@pytest.mark.parametrize(
    "path",
    [
        "/ws/voice/session-1",
        "/api/voip/voice/call-1",
        "/api/agents/agent-web/terminal",
        "/api/system-agent/terminal",
    ],
)
@pytest.mark.asyncio
async def test_mutating_websockets_are_rejected_by_an_active_release_lock(
    monkeypatch, path
):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    _, token = locks.acquire_lock(
        owner="release-test",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web"],
        ttl_seconds=900,
    )
    await locks.activate_lock_after_drain(token)
    downstream_called = False
    messages = []

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    await locks.DeploymentAdmissionMiddleware(downstream)(
        {"type": "websocket", "path": path, "headers": []}, receive, send
    )

    assert downstream_called is False
    assert messages == [
        {
            "type": "websocket.close",
            "code": 1013,
            "reason": "active deployment lock token is required",
        }
    ]


def test_read_only_websockets_do_not_require_mutation_admission():
    assert locks.websocket_requires_admission("/ws") is False
    assert locks.websocket_requires_admission("/ws/events") is False


def test_sqlalchemy_rejects_work_after_reservation_authority_loss(monkeypatch, tmp_path):
    from sqlalchemy import text
    from db.engine import _build_engine

    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    engine = _build_engine(f"sqlite:///{tmp_path / 'guarded.db'}")
    reservation = locks.begin_mutation(None)
    try:
        with locks.mutation_admission_context(reservation) as context:
            context.authority_error = locks.DeploymentLockRejected("reservation lost")
            with pytest.raises(locks.DeploymentLockRejected, match="reservation lost"):
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE blocked (id INTEGER)"))
    finally:
        locks.end_mutation(reservation)
        engine.dispose()


def test_sqlalchemy_rejects_commit_when_authority_is_lost_after_write(
    monkeypatch, tmp_path
):
    from sqlalchemy import text
    from db.engine import _build_engine

    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    engine = _build_engine(f"sqlite:///{tmp_path / 'commit-guarded.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE guarded (id INTEGER)"))

    reservation = locks.begin_mutation(None)
    try:
        with locks.mutation_admission_context(reservation) as context:
            with engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(text("INSERT INTO guarded (id) VALUES (1)"))
                context.authority_error = locks.DeploymentLockRejected(
                    "reservation lost before commit"
                )
                with pytest.raises(
                    locks.DeploymentLockRejected,
                    match="reservation lost before commit",
                ):
                    transaction.commit()
                transaction.rollback()
    finally:
        locks.end_mutation(reservation)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM guarded")).scalar_one() == 0
    engine.dispose()


def test_raw_sqlite_rejects_commit_when_authority_is_lost_after_write(
    monkeypatch, tmp_path
):
    from db.connection import connect_sqlite

    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    database_path = tmp_path / "raw-commit-guarded.db"
    with connect_sqlite(str(database_path)) as connection:
        connection.execute("CREATE TABLE guarded (id INTEGER)")

    reservation = locks.begin_mutation(None)
    try:
        with locks.mutation_admission_context(reservation) as context:
            connection = connect_sqlite(str(database_path))
            try:
                connection.execute("INSERT INTO guarded (id) VALUES (1)")
                context.authority_error = locks.DeploymentLockRejected(
                    "reservation lost before raw commit"
                )
                with pytest.raises(
                    locks.DeploymentLockRejected,
                    match="reservation lost before raw commit",
                ):
                    connection.commit()
                connection.rollback()
            finally:
                connection.close()
    finally:
        locks.end_mutation(reservation)

    with connect_sqlite(str(database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM guarded").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_database_vacuum_runs_off_the_event_loop(monkeypatch, tmp_path):
    from services import db_vacuum_service as vacuum_module

    event_loop_thread = threading.get_ident()
    execution_threads = []
    database_path = tmp_path / "vacuum.db"
    database_path.touch()

    class FakeConnection:
        def execute(self, statement):
            assert statement == "VACUUM"
            execution_threads.append(threading.get_ident())

        def close(self):
            return None

    monkeypatch.setattr(vacuum_module, "DB_PATH", str(database_path))
    monkeypatch.setattr(
        vacuum_module,
        "connect_sqlite",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    async def run(_executor, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    monkeypatch.setattr(locks, "run_governed_executor", run)

    result = await vacuum_module.DBVacuumService().vacuum()

    assert result["status"] == "ok"
    assert execution_threads and execution_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_cleanup_retries_startup_after_admission_release(monkeypatch):
    service = cleanup_module.CleanupService(poll_interval=0)
    startup = AsyncMock(
        side_effect=[locks.DeploymentLockRejected("release active"), None]
    )

    async def one_cycle():
        service._running = False
        return cleanup_module.CleanupReport()

    monkeypatch.setattr(service, "_run_startup_cleanup_governed", startup)
    monkeypatch.setattr(service, "run_cleanup_governed", one_cycle)
    service._running = True
    await service._cleanup_loop()

    assert startup.await_count == 2
    assert service._running is False


@pytest.mark.asyncio
async def test_retryable_startup_retries_initial_refresh_race(monkeypatch):
    redis = FakeRedis()
    authority = locks.MutationAdmissionAuthority(lambda: redis)
    real_refresh = authority.refresh
    refresh_attempts = 0
    operation_calls = 0

    def refresh(reservation):
        nonlocal refresh_attempts
        refresh_attempts += 1
        if refresh_attempts == 1:
            raise locks.DeploymentLockRejected("reservation lost before start")
        real_refresh(reservation)

    async def operation():
        nonlocal operation_calls
        operation_calls += 1
        return "ok"

    monkeypatch.setattr(authority, "refresh", refresh)

    result = await authority.run_when_admitted(operation, retry_seconds=0)

    assert result == "ok"
    assert operation_calls == 1
    assert authority.count(locks.IN_FLIGHT_KEY) == 0


def test_pure_asgi_gate_adopts_real_base_http_route_and_background_tasks(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    app = FastAPI()
    observed = {}

    @app.middleware("http")
    async def task_boundary(request: Request, call_next):
        observed["middleware_task"] = id(asyncio.current_task())
        return await call_next(request)

    @app.post("/mutate")
    async def mutate(background_tasks: BackgroundTasks):
        observed["route_task"] = id(asyncio.current_task())

        async def after_response():
            observed["background_count"] = redis.get(locks.IN_FLIGHT_KEY)

        background_tasks.add_task(locks.reserve_governed_call(after_response))
        return {"ok": True}

    gated = locks.DeploymentAdmissionMiddleware(app)
    with TestClient(gated) as client:
        response = client.post("/mutate")

    assert response.status_code == 200
    assert observed["middleware_task"] != observed["route_task"]
    assert observed["background_count"] == "2"
    assert redis.get(locks.IN_FLIGHT_KEY) is None


@pytest.mark.asyncio
async def test_candidate_rollback_requires_complete_absence_proof(
    monkeypatch, tmp_path
):
    agent_name = "agent-test-rollback"
    container = MagicMock()
    live_containers = [container, None]
    redis = FakeRedis()
    database = MagicMock()
    database.get_agent_owner.side_effect = [{"owner": "creator"}, None]
    database.delete_agent_ownership.return_value = True
    database.purge_agent_ownership.return_value = True
    database.is_agent_name_reserved.return_value = False
    database.get_connector_key_prefix.return_value = None
    database.get_agent_mcp_api_key.return_value = None
    database_proof = {
        "remainingRowCount": 0,
        "remainingKeepRowCount": 0,
        "remainingMcpKeyCount": 0,
        "remainingConnectorConfigCount": 0,
        "remainingByReference": {"agent_ownership:agent_name": 0},
    }
    monkeypatch.setattr(rollback, "db", database)
    monkeypatch.setattr(
        rollback, "get_agent_container", lambda _name: live_containers.pop(0)
    )
    monkeypatch.setattr(rollback, "container_stop", AsyncMock())
    monkeypatch.setattr(rollback, "container_remove", AsyncMock())
    monkeypatch.setattr(rollback, "remove_agent_volumes", AsyncMock(return_value=1))
    monkeypatch.setattr(
        rollback,
        "volume_get",
        AsyncMock(side_effect=docker.errors.NotFound("missing")),
    )
    monkeypatch.setattr(rollback, "get_breaker_redis", lambda: redis)
    credential_file = rollback._credential_artifacts(agent_name)[0]
    credential_file.write_text("secret", encoding="utf-8")
    capacity = MagicMock()
    capacity.cancel_all_overflow = AsyncMock()
    with patch(
        "services.capacity_manager.get_capacity_manager", return_value=capacity
    ), patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), patch(
        "db.agent_cleanup.purge_deployment_candidate_state", return_value=database_proof
    ):
        result = await rollback.rollback_candidate(agent_name)
    assert result["complete"] is True
    assert all(result["proof"].values())
    assert not credential_file.exists()


@pytest.mark.asyncio
async def test_candidate_rollback_reports_residual_volume(monkeypatch):
    agent_name = "agent-test-residual"
    redis = FakeRedis()
    database = MagicMock()
    database.get_agent_owner.side_effect = [{"owner": "creator"}, None]
    database.delete_agent_ownership.return_value = True
    database.purge_agent_ownership.return_value = True
    database.is_agent_name_reserved.return_value = False
    database.get_connector_key_prefix.return_value = None
    database.get_agent_mcp_api_key.return_value = None
    database_proof = {
        "remainingRowCount": 0,
        "remainingKeepRowCount": 0,
        "remainingMcpKeyCount": 0,
        "remainingConnectorConfigCount": 0,
        "remainingByReference": {"agent_ownership:agent_name": 0},
    }
    monkeypatch.setattr(rollback, "db", database)
    monkeypatch.setattr(rollback, "get_agent_container", lambda _name: None)
    monkeypatch.setattr(rollback, "remove_agent_volumes", AsyncMock(return_value=0))
    monkeypatch.setattr(rollback, "volume_get", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(rollback, "get_breaker_redis", lambda: redis)
    capacity = MagicMock()
    capacity.cancel_all_overflow = AsyncMock()
    with patch(
        "services.capacity_manager.get_capacity_manager", return_value=capacity
    ), patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), patch(
        "db.agent_cleanup.purge_deployment_candidate_state", return_value=database_proof
    ):
        result = await rollback.rollback_candidate(agent_name)
    assert result["complete"] is False
    assert result["proof"]["volumesAbsent"] is False
    assert len(result["remainingVolumes"]) == 3


def test_runtime_cleanup_is_exact_for_colliding_agent_names(monkeypatch):
    redis = FakeRedis()
    redis.runtime_keys = [
        "agent:circuit:agent-web",
        "agent:circuit:agent-web:probe-lock",
        "agent:slot:agent-web:execution-1",
        "agent:queue:agent-web",
        "agent:circuit:agent-web-2",
        "agent:slot:agent-web-2:execution-1",
        "agent:queue:agent-web-2",
    ]
    monkeypatch.setattr(rollback, "get_breaker_redis", lambda: redis)
    rollback._clear_deployment_runtime_keys("agent-web")
    assert rollback._remaining_runtime_keys("agent-web") == []
    assert sorted(redis.runtime_keys) == [
        "agent:circuit:agent-web-2",
        "agent:queue:agent-web-2",
        "agent:slot:agent-web-2:execution-1",
    ]
