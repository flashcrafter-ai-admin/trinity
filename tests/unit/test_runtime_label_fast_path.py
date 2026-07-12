"""Fast-path runtime-label read (#1187 review I6).

``list_all_agents_fast()`` builds AgentStatus from container LABELS only (no
``container.attrs`` inspect). It must read the runtime from the label that is
actually written at create time (``trinity.agent-runtime``, see
``crud.py``) — not ``trinity.runtime``, which is never written and so always
reports ``claude-code``, making the RuntimeBadge wrong for Codex/Gemini agents
in every fast-path view (monitoring, ops, telemetry).
"""

from __future__ import annotations

from types import SimpleNamespace

from services import docker_service


class _FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, **_kwargs):
        return self._containers


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


def _fake_container(labels: dict):
    return SimpleNamespace(
        labels=labels,
        name="agent-demo",
        status="running",
        id="container123",
    )


def test_fast_path_reads_agent_runtime_label(monkeypatch):
    labels = {
        "trinity.platform": "agent",
        "trinity.agent-type": "custom",
        "trinity.agent-runtime": "codex",
    }
    monkeypatch.setattr(
        docker_service, "docker_client", _FakeClient([_fake_container(labels)])
    )

    agents = docker_service.list_all_agents_fast()

    assert len(agents) == 1
    assert agents[0].runtime == "codex"


def test_fast_path_defaults_to_claude_when_label_absent(monkeypatch):
    labels = {"trinity.platform": "agent", "trinity.agent-type": "custom"}
    monkeypatch.setattr(
        docker_service, "docker_client", _FakeClient([_fake_container(labels)])
    )

    agents = docker_service.list_all_agents_fast()

    assert agents[0].runtime == "claude-code"
