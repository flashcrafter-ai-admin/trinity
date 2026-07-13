"""Real-Redis expiry, heartbeat, and generation-safety tests."""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
import redis

import deployment_admission as admission
from services import deployment_lock_service as locks


@pytest.fixture
def authority():
    url = os.getenv("TRINITY_ADMISSION_TEST_REDIS_URL")
    if not url:
        pytest.skip("TRINITY_ADMISSION_TEST_REDIS_URL is not configured")
    client = redis.Redis.from_url(url, decode_responses=True)
    client.flushdb()
    yield admission.MutationAdmissionAuthority(lambda: client), client
    client.flushdb()
    client.close()


def test_late_release_cannot_delete_a_new_generation(authority):
    gate, client = authority
    old = gate.begin(None)
    gate.end(old)
    current = gate.begin(None)

    gate.end(old)

    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 1
    assert client.zscore(current.key, current.reservation_id) is not None
    gate.end(current)


def test_synchronous_reservation_does_not_require_an_event_loop(authority):
    gate, _ = authority

    assert gate.run_sync(lambda: "ok") == "ok"
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 0


def test_synchronous_reservation_renews_until_callback_finishes(authority, monkeypatch):
    gate, _ = authority
    monkeypatch.setattr(admission, "RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(admission, "RESERVATION_HEARTBEAT_SECONDS", 0.05)
    started = threading.Event()
    finish = threading.Event()
    results = []

    def operation():
        started.set()
        assert finish.wait(timeout=3)
        return "ok"

    worker = threading.Thread(target=lambda: results.append(gate.run_sync(operation)))
    worker.start()
    assert started.wait(timeout=1)
    time.sleep(1.2)
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 1
    finish.set()
    worker.join(timeout=2)

    assert results == ["ok"]
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 0


def test_synchronous_heartbeat_loss_reaches_callback(authority, monkeypatch):
    gate, client = authority
    monkeypatch.setattr(admission, "RESERVATION_HEARTBEAT_SECONDS", 0.05)
    started = threading.Event()
    finish = threading.Event()
    errors = []

    def operation():
        started.set()
        assert finish.wait(timeout=3)
        admission.assert_current_authority()

    def run():
        try:
            gate.run_sync(operation)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=1)
    reservation_id = client.zrange(admission.ORDINARY_RESERVATIONS_KEY, 0, 0)[0]
    client.zrem(admission.ORDINARY_RESERVATIONS_KEY, reservation_id)

    deadline = time.time() + 1
    while gate.count(admission.ORDINARY_RESERVATIONS_KEY) != 0 and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    finish.set()
    worker.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], admission.DeploymentLockRejected)
    assert "was lost" in str(errors[0])


@pytest.mark.asyncio
async def test_heartbeat_keeps_long_operation_visible(authority, monkeypatch):
    gate, _ = authority
    monkeypatch.setattr(admission, "RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(admission, "RESERVATION_HEARTBEAT_SECONDS", 0.1)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def operation():
        started.set()
        await finish.wait()

    task = gate.spawn(operation())
    await started.wait()
    await asyncio.sleep(1.2)
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 1
    finish.set()
    await task
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 0


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_the_mutation_owner(authority, monkeypatch):
    gate, client = authority
    monkeypatch.setattr(admission, "RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(admission, "RESERVATION_HEARTBEAT_SECONDS", 0.05)
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.Event().wait()

    task = gate.spawn(operation())
    await started.wait()
    reservation_id = client.zrange(admission.ORDINARY_RESERVATIONS_KEY, 0, 0)[0]
    client.zrem(admission.ORDINARY_RESERVATIONS_KEY, reservation_id)

    with pytest.raises(admission.DeploymentLockRejected, match="was lost"):
        await asyncio.wait_for(task, timeout=1)
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 0


@pytest.mark.asyncio
async def test_cancelled_executor_waits_for_effect_before_ending_child(authority):
    gate, _ = authority
    started = threading.Event()
    finish = threading.Event()

    def effect():
        started.set()
        assert finish.wait(timeout=3)

    parent = gate.begin(None)
    with gate.context(parent):
        task = asyncio.create_task(gate.run_in_executor(None, effect))
        assert await asyncio.to_thread(started.wait, 1)
    gate.end(parent)
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 1

    task.cancel()
    await asyncio.sleep(0.05)
    assert task.done() is False
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 1
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 0


@pytest.mark.asyncio
async def test_candidate_mutex_serializes_and_unenrolls_on_real_redis(
    authority,
    monkeypatch,
):
    _, client = authority
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: client)
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
    rollback_started = asyncio.Event()

    async def create():
        async with locks.candidate_operation(token, "agent-web"):
            create_started.set()
            await allow_create.wait()

    async def rollback():
        async with locks.candidate_operation(token, "agent-web"):
            rollback_started.set()
            locks.unenroll_candidate(token, "agent-web")

    create_task = asyncio.create_task(create())
    await create_started.wait()
    rollback_task = asyncio.create_task(rollback())
    await asyncio.sleep(0.05)
    assert rollback_started.is_set() is False
    allow_create.set()
    await create_task
    await rollback_task

    with pytest.raises(locks.DeploymentLockRejected, match="not enrolled"):
        locks.require_enrolled_candidate(token, "agent-web")


def test_expired_generation_cannot_damage_replacement(authority, monkeypatch):
    gate, client = authority
    monkeypatch.setattr(admission, "RESERVATION_TTL_SECONDS", 1)
    expired = gate.begin(None)
    client.zadd(expired.key, {expired.reservation_id: 0})
    replacement = gate.begin(None)

    gate.end(expired)

    assert gate.count(admission.ORDINARY_RESERVATIONS_KEY) == 1
    assert client.zscore(replacement.key, replacement.reservation_id) is not None
