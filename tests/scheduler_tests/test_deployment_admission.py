"""Scheduler integration checks for the shared deployment admission authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis

import deployment_admission as admission
from scheduler.service import SchedulerService
from scheduler.main import SchedulerApp
from scheduler.database import SchedulerDatabase


@pytest.fixture
def real_admission():
    url = os.getenv("TRINITY_ADMISSION_TEST_REDIS_URL")
    if not url:
        pytest.skip("TRINITY_ADMISSION_TEST_REDIS_URL is not configured")
    client = redis.Redis.from_url(url, decode_responses=True)
    client.flushdb()
    yield admission.MutationAdmissionAuthority(lambda: client), client, url
    client.flushdb()
    client.close()


def _locked_state(phase: str) -> str:
    return json.dumps(
        {
            "owner": "test-release",
            "fleetLockDigest": "a" * 64,
            "agentNames": ["agent-web"],
            "phase": phase,
            "tokenDigest": hashlib.sha256(b"release-token").hexdigest(),
            "ttlSeconds": 900,
        },
        sort_keys=True,
    )


def test_scheduler_redis_client_uses_bounded_socket_timeouts(monkeypatch):
    from scheduler import service as scheduler_service

    monkeypatch.setattr(scheduler_service.config, "redis_socket_connect_timeout", 2.5)
    monkeypatch.setattr(scheduler_service.config, "redis_socket_timeout", 4.5)
    redis_client = MagicMock()

    with patch("scheduler.service.redis.from_url", return_value=redis_client) as from_url:
        service = SchedulerService(
            database=MagicMock(),
            lock_manager=MagicMock(),
            redis_url="redis://test:test@redis:6379",
        )

        assert service.redis is redis_client

    from_url.assert_called_once_with(
        "redis://test:test@redis:6379",
        decode_responses=True,
        socket_connect_timeout=2.5,
        socket_timeout=4.5,
    )


@pytest.mark.parametrize("redis_error", [redis.ConnectionError, redis.TimeoutError])
def test_scheduler_admission_redis_failures_are_reported_as_unavailable(redis_error):
    redis_client = MagicMock()
    redis_client.get.side_effect = redis_error("redis unavailable")
    database = MagicMock()

    with patch("scheduler.service.redis.from_url", return_value=redis_client):
        service = SchedulerService(
            database=database,
            lock_manager=MagicMock(),
            redis_url="redis://test:test@redis:6379",
        )
        service._admission = admission.MutationAdmissionAuthority(
            lambda: service._admission_redis
        )

        with pytest.raises(admission.DeploymentLockUnavailable) as exc_info:
            service.initialize()

    assert isinstance(exc_info.value.__cause__, redis_error)
    database.ensure_process_schedules_table.assert_not_called()


@pytest.mark.parametrize("phase", ["draining", "active", "releasing"])
def test_scheduler_initialization_fails_closed_while_release_is_locked(
    real_admission, phase
):
    gate, client, url = real_admission
    client.set(admission.LOCK_KEY, _locked_state(phase))
    database = MagicMock()
    service = SchedulerService(database=database, redis_url=url)
    service._admission = gate

    with pytest.raises(admission.DeploymentLockRejected):
        service.initialize()

    database.ensure_process_schedules_table.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_execution_fails_closed_before_lock_or_database_mutation(
    real_admission,
):
    gate, client, url = real_admission
    client.set(admission.LOCK_KEY, _locked_state("active"))
    database = MagicMock()
    lock_manager = MagicMock()
    service = SchedulerService(
        database=database,
        lock_manager=lock_manager,
        redis_url=url,
    )
    service._admission = gate

    with pytest.raises(admission.DeploymentLockRejected):
        await service._execute_schedule("schedule-1")

    lock_manager.try_acquire_schedule_lock.assert_not_called()
    database.get_schedule.assert_not_called()


def test_scheduler_database_commit_fails_closed_after_authority_loss(
    real_admission, monkeypatch, tmp_path
):
    gate, client, _ = real_admission
    monkeypatch.setattr(admission, "RESERVATION_HEARTBEAT_SECONDS", 0.05)
    database_path = tmp_path / "scheduler-admission.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE guarded (id INTEGER)")

    database = SchedulerDatabase(str(database_path))
    started = threading.Event()
    finish = threading.Event()
    errors = []

    def operation():
        with database.get_connection() as connection:
            connection.execute("INSERT INTO guarded (id) VALUES (1)")
            started.set()
            assert finish.wait(timeout=3)
            connection.commit()

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
    time.sleep(0.1)
    finish.set()
    worker.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], admission.DeploymentLockRejected)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM guarded").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_periodic_sync_pauses_instead_of_terminating_scheduler(monkeypatch):
    app = SchedulerApp()
    service = MagicMock()
    service._instance_id = "scheduler-test"
    service.lock_manager = MagicMock()
    service._sync_schedules = AsyncMock(
        side_effect=admission.DeploymentLockRejected("release active")
    )
    app.scheduler_service = service

    from scheduler import main as scheduler_main

    monkeypatch.setattr(scheduler_main.config, "schedule_reload_interval", 0)

    async def stop_after_cycle(_seconds):
        app._shutdown_event.set()

    monkeypatch.setattr(scheduler_main.asyncio, "sleep", stop_after_cycle)
    await app._run_until_shutdown()

    service._sync_schedules.assert_awaited_once()
    service.lock_manager.set_heartbeat.assert_called_once_with("scheduler-test")
