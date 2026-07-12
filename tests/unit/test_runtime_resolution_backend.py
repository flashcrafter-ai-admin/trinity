"""Backend runtime resolution (#1187 F-MCP).

The backend resolves an agent's runtime to make the platform system prompt
runtime-aware (Codex strips the Claude-only ``mcp__trinity__`` tool-name
prefix). Two seams cover this, both **best-effort, never-raise, Claude-default**:

  * ``docker_service.get_agent_runtime(name)`` reads the ``trinity.agent-runtime``
    container label, and
  * ``task_execution_service._resolve_agent_runtime(name)`` wraps it behind a
    guarded local import so a unit-test stub of ``services.docker_service`` (or a
    missing symbol / Docker outage) can never block dispatch.

Neither may raise or block dispatch — any failure falls back to ``claude-code``,
preserving the historical Claude/Gemini prompt naming.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from services import docker_service
from services.task_execution_service import _resolve_agent_runtime


# ---------------------------------------------------------------------------
# docker_service.get_agent_runtime
# ---------------------------------------------------------------------------

def test_get_agent_runtime_reads_label(monkeypatch):
    container = SimpleNamespace(labels={"trinity.agent-runtime": "codex"})
    monkeypatch.setattr(docker_service, "get_agent_container", lambda name: container)
    assert docker_service.get_agent_runtime("demo") == "codex"


def test_get_agent_runtime_defaults_when_label_absent(monkeypatch):
    container = SimpleNamespace(labels={"trinity.platform": "agent"})
    monkeypatch.setattr(docker_service, "get_agent_container", lambda name: container)
    assert docker_service.get_agent_runtime("demo") == "claude-code"


def test_get_agent_runtime_defaults_when_container_missing(monkeypatch):
    monkeypatch.setattr(docker_service, "get_agent_container", lambda name: None)
    assert docker_service.get_agent_runtime("ghost") == "claude-code"


def test_get_agent_runtime_never_raises(monkeypatch):
    """A Docker hiccup mid-read (labels access throws) falls back to claude-code,
    never propagating — runtime resolution must not block dispatch."""

    class _RaisingLabels:
        def get(self, *a, **k):
            raise RuntimeError("docker hiccup")

    monkeypatch.setattr(
        docker_service,
        "get_agent_container",
        lambda name: SimpleNamespace(labels=_RaisingLabels()),
    )
    assert docker_service.get_agent_runtime("demo") == "claude-code"


# ---------------------------------------------------------------------------
# task_execution_service._resolve_agent_runtime — the guarded wrapper
# ---------------------------------------------------------------------------

def test_resolve_agent_runtime_delegates_to_docker_service(monkeypatch):
    # _resolve_agent_runtime does a *local* `from services.docker_service import
    # get_agent_runtime` at call time, reading sys.modules. Other unit tests
    # (e.g. test_voice_*) install a MagicMock at sys.modules["services.docker_
    # service"] that the conftest restore doesn't clear, so patching a module
    # reference captured at import time misses what the function actually reads.
    # Control sys.modules directly (auto-restored) — bulletproof regardless of
    # any leaked stub (the documented module-identity gotcha).
    fake_mod = types.SimpleNamespace(get_agent_runtime=lambda name: "codex")
    monkeypatch.setitem(sys.modules, "services.docker_service", fake_mod)
    assert _resolve_agent_runtime("demo") == "codex"


def test_resolve_agent_runtime_falls_back_on_error(monkeypatch):
    """If the docker_service lookup fails (a partial stub lacking the symbol, or
    a Docker outage), resolution degrades to claude-code rather than raising."""
    # A module object with NO get_agent_runtime → the local from-import raises
    # ImportError, which the guard swallows (the "partial stub" scenario).
    monkeypatch.setitem(sys.modules, "services.docker_service", types.SimpleNamespace())
    assert _resolve_agent_runtime("demo") == "claude-code"
