"""Governed fleet deployment lease and rollback regression tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import docker
import pytest

from services import deployment_lock_service as locks
from services import deployment_rollback_service as rollback


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.runtime_keys = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, _script, _key_count, key, expected):
        if self.values.get(key) != expected:
            return 0
        del self.values[key]
        return 1

    def scan_iter(self, match):
        import fnmatch

        return (key for key in self.runtime_keys if fnmatch.fnmatch(key, match))


def test_deployment_lock_is_exclusive_scoped_and_atomically_released(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_breaker_redis", lambda: redis)
    state, token = locks.acquire_lock(
        owner="creator",
        fleet_lock_digest="a" * 64,
        agent_names=["agent-web", "agent-seo"],
        ttl_seconds=900,
    )
    assert state["agentNames"] == ["agent-seo", "agent-web"]
    assert locks.require_mutation_admission(token)["fleetLockDigest"] == "a" * 64
    assert locks.require_candidate(token, "agent-web")["owner"] == "creator"
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
    assert locks.release_lock(token)["fleetLockDigest"] == "a" * 64
    assert locks.require_mutation_admission(None) is None
    with pytest.raises(locks.DeploymentLockRejected):
        locks.require_mutation_admission(token)


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
    ],
)
def test_all_agent_affecting_mutation_surfaces_require_admission(path):
    assert locks.mutation_requires_admission("POST", path) is True
    assert locks.mutation_requires_admission("GET", path) is False


def test_lock_acquisition_is_the_only_unlocked_agent_mutation():
    assert (
        locks.mutation_requires_admission("POST", "/api/agents/deployment-lock")
        is False
    )
    assert (
        locks.mutation_requires_admission("DELETE", "/api/agents/deployment-lock")
        is True
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
    ), patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()):
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
    monkeypatch.setattr(rollback, "db", database)
    monkeypatch.setattr(rollback, "get_agent_container", lambda _name: None)
    monkeypatch.setattr(rollback, "remove_agent_volumes", AsyncMock(return_value=0))
    monkeypatch.setattr(rollback, "volume_get", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(rollback, "get_breaker_redis", lambda: redis)
    capacity = MagicMock()
    capacity.cancel_all_overflow = AsyncMock()
    with patch(
        "services.capacity_manager.get_capacity_manager", return_value=capacity
    ), patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()):
        result = await rollback.rollback_candidate(agent_name)
    assert result["complete"] is False
    assert result["proof"]["volumesAbsent"] is False
    assert len(result["remainingVolumes"]) == 3
