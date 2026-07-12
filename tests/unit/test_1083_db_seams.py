"""Unit tests for the #1083 fire-and-forget DB seams.

Two DB-layer primitives the callback endpoint and lease reaper depend on:

1. ``ActivityOperations.get_open_activity_id_for_execution`` (Codex #8) — must
   return ONLY the open dispatch activity (chat_start / schedule_start, state
   'started') for an execution id, never a tool_call/collaboration row that
   shares the same ``related_execution_id``. An unfiltered lookup would close
   the wrong activity.

2. ``ScheduleOperations.mark_execution_dispatched(async_dispatch=True)`` — writes
   the durable ``'dispatched_async'`` marker the callback gates on (fail-closed),
   while leaving the legacy ``'dispatched'`` sentinel unchanged for the sync path.

Real-SQLite (and PostgreSQL when TEST_POSTGRES_URL is set) via db_harness (#300).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"

from db_harness import db_backend, run as _hrun  # noqa: E402,F401  (db_backend is a pytest fixture)


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    """Active backend with a fresh full production schema (db_harness, #300)."""
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


@pytest.fixture
def activity_ops(tmp_db):
    from db.activities import ActivityOperations

    return ActivityOperations()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_running(*, exec_id: str, claude_session_id, started_at: str):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by, claude_session_id) "
        "VALUES (:id, '__manual__', 'test-agent', 'running', :sa, 'msg', 'schedule', :csid)",
        id=exec_id, sa=started_at, csid=claude_session_id,
    )


def _insert_activity(
    *, act_id: str, exec_id: str, activity_type: str, activity_state: str, created_at: str
):
    _hrun(
        "INSERT INTO agent_activities "
        "(id, agent_name, activity_type, activity_state, started_at, "
        " triggered_by, related_execution_id, created_at) "
        "VALUES (:id, 'test-agent', :atype, :astate, :sa, 'schedule', :eid, :ca)",
        id=act_id, atype=activity_type, astate=activity_state,
        sa=created_at, eid=exec_id, ca=created_at,
    )


class TestGetOpenActivityIdForExecution:
    pytestmark = pytest.mark.unit

    def test_returns_open_chat_start_activity(self, tmp_db, activity_ops):
        _insert_activity(
            act_id="act-1", exec_id="exec-1",
            activity_type="chat_start", activity_state="started",
            created_at=_now_iso(),
        )
        assert activity_ops.get_open_activity_id_for_execution("exec-1") == "act-1"

    def test_returns_open_schedule_start_activity(self, tmp_db, activity_ops):
        _insert_activity(
            act_id="act-sched", exec_id="exec-sched",
            activity_type="schedule_start", activity_state="started",
            created_at=_now_iso(),
        )
        assert activity_ops.get_open_activity_id_for_execution("exec-sched") == "act-sched"

    def test_ignores_tool_call_row_sharing_execution_id(self, tmp_db, activity_ops):
        """Codex #8: a tool_call row shares related_execution_id but must NOT be
        returned — only the dispatch (chat/schedule_start) activity is closeable
        by the callback/reaper."""
        # Earlier-created dispatch row + a later tool_call row sharing the eid.
        base = datetime.now(timezone.utc)
        _insert_activity(
            act_id="act-dispatch", exec_id="exec-shared",
            activity_type="chat_start", activity_state="started",
            created_at=(base - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        _insert_activity(
            act_id="act-tool", exec_id="exec-shared",
            activity_type="tool_call", activity_state="started",
            created_at=base.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        # Even though the tool_call row is newest, the filter excludes it.
        assert activity_ops.get_open_activity_id_for_execution("exec-shared") == "act-dispatch"

    def test_ignores_completed_activity(self, tmp_db, activity_ops):
        _insert_activity(
            act_id="act-done", exec_id="exec-done",
            activity_type="chat_start", activity_state="completed",
            created_at=_now_iso(),
        )
        assert activity_ops.get_open_activity_id_for_execution("exec-done") is None

    def test_returns_none_when_no_activity(self, tmp_db, activity_ops):
        assert activity_ops.get_open_activity_id_for_execution("nope") is None


class TestMarkExecutionDispatchedAsync:
    pytestmark = pytest.mark.unit

    def _fetch_csid(self, exec_id: str):
        from sqlalchemy import text
        from db.engine import get_engine

        with get_engine().connect() as conn:
            return conn.execute(
                text("SELECT claude_session_id FROM schedule_executions WHERE id = :id"),
                {"id": exec_id},
            ).scalar()

    def test_async_marker_writes_dispatched_async(self, tmp_db, schedule_ops):
        _insert_running(exec_id="exec-async", claude_session_id=None, started_at=_now_iso())
        assert schedule_ops.mark_execution_dispatched("exec-async", async_dispatch=True) is True
        assert self._fetch_csid("exec-async") == "dispatched_async"

    def test_sync_marker_unchanged(self, tmp_db, schedule_ops):
        _insert_running(exec_id="exec-sync", claude_session_id=None, started_at=_now_iso())
        assert schedule_ops.mark_execution_dispatched("exec-sync") is True
        assert self._fetch_csid("exec-sync") == "dispatched"

    def test_async_marker_guarded_on_null_session(self, tmp_db, schedule_ops):
        """Same NULL guard as the sync sentinel — a second mark is a no-op."""
        _insert_running(exec_id="exec-guard", claude_session_id=None, started_at=_now_iso())
        assert schedule_ops.mark_execution_dispatched("exec-guard", async_dispatch=True) is True
        assert schedule_ops.mark_execution_dispatched("exec-guard", async_dispatch=True) is False

    def test_async_marker_not_failed_by_no_session_sweep(self, tmp_db, schedule_ops):
        """'dispatched_async' is non-NULL/non-empty, so the silent-launch sweep
        treats it identically to 'dispatched' (it survives)."""
        stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S")
        _insert_running(exec_id="exec-async-stale", claude_session_id=None, started_at=stale)
        schedule_ops.mark_execution_dispatched("exec-async-stale", async_dispatch=True)
        assert schedule_ops.mark_no_session_executions_failed(timeout_seconds=60) == 0
