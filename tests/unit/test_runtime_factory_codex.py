"""Runtime factory tests for the Codex runtime + unknown-runtime validation (#1187).

``get_runtime()`` must:
  * return a CodexRuntime when AGENT_RUNTIME=codex,
  * keep defaulting to Claude when AGENT_RUNTIME is unset (back-compat),
  * but RAISE on an explicitly-set unknown value instead of silently selecting
    Claude — a typo'd runtime should fail loudly, not run the wrong engine.
"""

from __future__ import annotations

import pytest

from agent_server.services.runtime_adapter import (  # noqa: E402
    KNOWN_RUNTIMES,
    get_runtime,
)
from agent_server.services.codex_runtime import CodexRuntime  # noqa: E402


def test_runtime_factory_codex(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "codex")
    assert isinstance(get_runtime(), CodexRuntime)


def test_runtime_factory_unknown_raises(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "totally-bogus-runtime")
    with pytest.raises(ValueError) as exc_info:
        get_runtime()
    # The error must name the offending value and not silently fall back.
    assert "totally-bogus-runtime" in str(exc_info.value)


def test_runtime_factory_default_is_claude(monkeypatch):
    """Env unset → Claude Code (unchanged back-compat). The empty-default path
    must NOT raise."""
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    runtime = get_runtime()
    assert runtime.__class__.__name__ == "ClaudeCodeRuntime"


def test_codex_is_a_known_runtime():
    assert "codex" in KNOWN_RUNTIMES
