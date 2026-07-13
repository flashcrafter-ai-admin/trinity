"""Scheduler integration checks for the shared deployment admission authority."""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import MagicMock

import pytest
import redis

import deployment_admission as admission
from scheduler.service import SchedulerService


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
