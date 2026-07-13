"""Fail-closed hard rollback for candidates created under a deployment lease."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import docker

from database import db
from redis_breaker_util import get_breaker_redis
from services.docker_service import get_agent_container
from services.docker_utils import (
    container_remove,
    container_stop,
    remove_agent_volumes,
    volume_get,
)

_VOLUME_SUFFIXES = ("workspace", "public", "shared")
async def _remaining_volumes(agent_name: str) -> List[str]:
    remaining: List[str] = []
    for suffix in _VOLUME_SUFFIXES:
        name = f"agent-{agent_name}-{suffix}"
        try:
            await volume_get(name)
            remaining.append(name)
        except docker.errors.NotFound:
            continue
    return remaining


def _runtime_keys(agent_name: str, client) -> List[str]:
    exact = {
        f"agent:heartbeat:{agent_name}",
        f"agent:heartbeat:seen:{agent_name}",
        f"agent:heartbeat:misses:{agent_name}",
        f"agent:circuit:{agent_name}",
        f"agent:circuit:{agent_name}:probe-lock",
        f"agent:dispatch:{agent_name}",
        f"agent:dispatch:{agent_name}:probe-lock",
        f"agent:slots:{agent_name}",
        f"agent:queue:{agent_name}",
        f"agent:data_op:{agent_name}",
    }
    metadata_prefix = f"agent:slot:{agent_name}:"
    found = {
        key.decode() if isinstance(key, bytes) else str(key)
        for key in client.scan_iter(match=f"{metadata_prefix}*")
    }
    for exact_key in exact:
        for key in client.scan_iter(match=exact_key):
            found.add(key.decode() if isinstance(key, bytes) else str(key))
    return sorted(found)


def _remaining_runtime_keys(agent_name: str) -> List[str]:
    client = get_breaker_redis()
    if client is None:
        raise RuntimeError("runtime-state authority is unavailable")
    return _runtime_keys(agent_name, client)


def _clear_deployment_runtime_keys(agent_name: str) -> None:
    client = get_breaker_redis()
    if client is None:
        raise RuntimeError("runtime-state authority is unavailable")
    keys = _runtime_keys(agent_name, client)
    if keys:
        client.delete(*keys)


def _credential_artifacts(agent_name: str) -> List[Path]:
    return [
        Path(f"/tmp/agent-{agent_name}-credentials.json"),
        Path(f"/tmp/agent-{agent_name}-creds"),
        Path(f"/tmp/agent-{agent_name}.yaml"),
    ]


async def rollback_candidate(agent_name: str) -> Dict[str, Any]:
    errors: List[str] = []
    container = get_agent_container(agent_name)
    if container is not None:
        try:
            await container_stop(container)
        except Exception as exc:  # noqa: BLE001 - proof below remains authoritative
            errors.append(f"container stop: {exc}")
        try:
            await container_remove(container, force=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"container remove: {exc}")

    try:
        from services.capacity_manager import get_capacity_manager

        await get_capacity_manager().cancel_all_overflow(
            agent_name, reason="deployment_rollback"
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"backlog clear: {exc}")

    try:
        from services.agent_runtime_state import clear_agent_runtime_state

        await clear_agent_runtime_state(agent_name)
        _clear_deployment_runtime_keys(agent_name)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"runtime clear: {exc}")

    try:
        from db.agent_cleanup import purge_deployment_candidate_state

        database_proof = purge_deployment_candidate_state(agent_name)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"database purge: {exc}")
        database_proof = {
            "remainingRowCount": -1,
            "remainingKeepRowCount": -1,
            "remainingMcpKeyCount": -1,
            "remainingConnectorConfigCount": -1,
            "remainingByReference": {"proof": -1},
        }

    try:
        await remove_agent_volumes(agent_name)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"volume removal: {exc}")

    for artifact in _credential_artifacts(agent_name):
        try:
            if artifact.is_dir():
                shutil.rmtree(artifact)
            else:
                artifact.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"artifact removal: {artifact.name}: {exc}")

    try:
        remaining_volumes = await _remaining_volumes(agent_name)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"volume proof: {exc}")
        remaining_volumes = ["proof-unavailable"]
    try:
        remaining_runtime_keys = _remaining_runtime_keys(agent_name)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"runtime-state proof: {exc}")
        remaining_runtime_keys = ["proof-unavailable"]
    proof = {
        "containerAbsent": get_agent_container(agent_name) is None,
        "agentRowsAbsent": database_proof["remainingRowCount"] == 0,
        "ownershipAbsent": database_proof["remainingByReference"].get(
            "agent_ownership:agent_name", -1
        ) == 0,
        "allMcpKeysAbsent": database_proof["remainingMcpKeyCount"] == 0,
        "connectorConfigAbsent": database_proof["remainingConnectorConfigCount"] == 0,
        "retainedHistoryAbsent": database_proof["remainingKeepRowCount"] == 0,
        "credentialArtifactsAbsent": not any(
            path.exists() for path in _credential_artifacts(agent_name)
        ),
        "runtimeStateAbsent": len(remaining_runtime_keys) == 0,
        "dataOperationStateAbsent": len(remaining_runtime_keys) == 0,
        "volumesAbsent": len(remaining_volumes) == 0,
    }
    complete = all(proof.values()) and not errors
    return {
        "agentName": agent_name,
        "complete": complete,
        "proof": proof,
        "remainingVolumes": remaining_volumes,
        "remainingRuntimeKeyCount": len(remaining_runtime_keys),
        "remainingRuntimeKeys": remaining_runtime_keys,
        "remainingDatabaseRows": database_proof["remainingByReference"],
        "errors": errors,
    }
