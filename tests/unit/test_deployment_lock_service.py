"""Governed fleet deployment lease and rollback regression tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import docker
import pytest

from services import deployment_lock_service as locks
from services import deployment_rollback_service as rollback
from services import cleanup_service as cleanup_module
from services import slot_service as slot_module


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.runtime_keys = []
        self.sets = {}

    def get(self, key):
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
            if key in self.runtime_keys:
                self.runtime_keys.remove(key)

    def expire(self, _key, _seconds):
        return True

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def eval(self, script, key_count, *args):
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
    assert counted == locks.IN_FLIGHT_KEY
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
    assert lease_counter.startswith(locks.LEASE_IN_FLIGHT_PREFIX)
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
        assert redis.values[locks.IN_FLIGHT_KEY] == "1"
        return "done"

    assert await job() == "done"
    assert locks.IN_FLIGHT_KEY not in redis.values
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
async def test_raw_task_cannot_reuse_parent_admission_context(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    observed = asyncio.Event()
    finish = asyncio.Event()

    @locks.governed_background_mutation
    async def child():
        assert redis.values[locks.IN_FLIGHT_KEY] == "2"
        observed.set()
        await finish.wait()

    parent_counter = locks.begin_mutation(None)
    with locks.mutation_admission_context(parent_counter):
        task = asyncio.create_task(child())
        await observed.wait()
    locks.end_mutation(parent_counter)
    assert redis.values[locks.IN_FLIGHT_KEY] == "1"
    finish.set()
    await task
    assert locks.IN_FLIGHT_KEY not in redis.values


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
    assert redis.values[locks.IN_FLIGHT_KEY] == "2"
    locks.end_mutation(parent_counter)
    assert redis.values[locks.IN_FLIGHT_KEY] == "1"
    callback_finish.set()
    await callback_done.wait()
    await asyncio.sleep(0)
    assert locks.IN_FLIGHT_KEY not in redis.values


@pytest.mark.asyncio
async def test_cleanup_startup_write_and_first_sweep_share_one_admission(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    observed = []
    monkeypatch.setattr(
        cleanup_module.db,
        "mark_orphan_loops_interrupted",
        lambda: observed.append(redis.values.get(locks.IN_FLIGHT_KEY)) or 0,
    )
    service = cleanup_module.CleanupService(poll_interval=1)

    async def first_sweep():
        observed.append(redis.values.get(locks.IN_FLIGHT_KEY))
        return cleanup_module.CleanupReport()

    monkeypatch.setattr(service, "run_cleanup", first_sweep)
    await service._run_startup_cleanup_governed()
    assert observed == ["1", "1"]
    assert locks.IN_FLIGHT_KEY not in redis.values


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


def test_lock_lifecycle_endpoints_are_the_only_special_admission_paths():
    assert (
        locks.mutation_requires_admission("POST", "/api/agents/deployment-lock")
        is False
    )
    assert (
        locks.mutation_requires_admission("DELETE", "/api/agents/deployment-lock")
        is False
    )


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
