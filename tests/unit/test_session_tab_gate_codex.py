"""Session-tab runtime gate (#1187 Phase H).

The cached-UUID ``--resume`` turn is gated so a Codex agent runs a stateless
turn instead. The gate must:
  * recognize codex as a non-resume runtime,
  * leave Claude (and Gemini, in the MVP) resume-capable,
  * fail safe (assume resume-capable) on any Docker lookup hiccup.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path


# `from routers import sessions` would execute routers/__init__.py, which eagerly
# imports all 50+ routers — including routers/agents.py → `from
# services.agent_service import get_agents_by_prefix`. Sibling unit tests
# (e.g. test_inject_assigned_credentials.py) install a stub `services.agent_service`
# in sys.modules at collection time, so under `-p randomly` that broad import
# raises ImportError while *this* module is being collected (the #1187
# regression-diff failure). Exec'ing sessions.py in isolation — whose own
# dependency chain never reaches services.agent_service — sidesteps the
# pollution without mutating sys.modules. Absolute imports inside sessions.py
# resolve via sys.path (conftest puts src/backend on it).
def _load_sessions_router() -> types.ModuleType:
    for base in (
        Path(__file__).resolve().parents[2] / "src" / "backend",  # host / CI
        Path("/app"),  # trinity-backend container
    ):
        path = base / "routers" / "sessions.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location(
                "routers_sessions_under_test", str(path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module
    raise RuntimeError("Cannot locate routers/sessions.py")


sessions = _load_sessions_router()


def _status(runtime):
    return types.SimpleNamespace(runtime=runtime)


def test_codex_in_no_resume_constant():
    assert "codex" in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME


def test_supports_resume_false_for_codex(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("codex")
    )
    assert sessions._supports_session_tab_resume("a") is False


def test_supports_resume_true_for_claude(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("claude-code")
    )
    assert sessions._supports_session_tab_resume("a") is True


def test_supports_resume_true_for_gemini_in_mvp(monkeypatch):
    """Only codex is gated in the MVP — Gemini keeps its (existing) Session tab."""
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("gemini-cli")
    )
    assert sessions._supports_session_tab_resume("a") is True


def test_supports_resume_true_when_container_missing(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: None)
    assert sessions._supports_session_tab_resume("a") is True


def test_supports_resume_defaults_true_on_lookup_failure(monkeypatch):
    def _boom(name):
        raise RuntimeError("docker socket down")

    monkeypatch.setattr(sessions, "get_agent_container", _boom)
    # Must not raise, and must fail safe to resume-capable.
    assert sessions._supports_session_tab_resume("a") is True
