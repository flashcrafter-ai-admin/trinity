"""Tests for #1265 Dashboard/timeline fleet-scale perf fixes.

Covers the bulk DB methods that replaced per-agent / per-schedule N+1
fan-out on the Dashboard load path, plus the timeline SQL-side access
filter:

  * ScheduleOperations.get_latest_execution_per_schedule (bulk, /ops/schedules)
  * ActivityOperations.get_latest_activity_for_agents (bulk, /context-stats)
  * ActivityOperations.get_activities_in_range(agent_names=...) (timeline)

Same backend-agnostic harness + sys.modules pollution defence as
tests/unit/test_schedule_analytics.py — runs on SQLite always, and on
PostgreSQL when TEST_POSTGRES_URL is set (the window functions are the
reason to exercise both).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import (  # noqa: E402
    db_backend,
    seed_user,
    seed_schedule,
    seed_execution,
    _engine,
)

_STUBBED_MODULE_NAMES = ["db.schedules", "db.activities", "db.users", "db.agents"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    names = _STUBBED_MODULE_NAMES + ["db.connection"]
    saved = {n: sys.modules.get(n) for n in names}
    db_pkg = sys.modules.get("db")
    saved_attrs = (
        {n.split(".", 1)[1]: getattr(db_pkg, n.split(".", 1)[1], None) for n in names}
        if db_pkg is not None
        else {}
    )
    for name in _STUBBED_MODULE_NAMES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        for attr, value in saved_attrs.items():
            if value is None:
                if db_pkg is not None and hasattr(db_pkg, attr):
                    delattr(db_pkg, attr)
            elif db_pkg is not None:
                setattr(db_pkg, attr, value)


def _seed_activity(activity_id, agent_name, created_at, state="started"):
    from sqlalchemy import text
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_activities "
                "(id, agent_name, activity_type, activity_state, started_at, "
                " triggered_by, created_at) "
                "VALUES (:id, :a, 'chat_start', :st, :ca, 'user', :ca)"
            ),
            {"id": activity_id, "a": agent_name, "st": state, "ca": created_at},
        )


# ---------------------------------------------------------------------------
# get_latest_execution_per_schedule
# ---------------------------------------------------------------------------

@pytest.fixture
def sched_ops(db_backend):
    seed_user(1, "owner", "user")
    from db.schedules import ScheduleOperations
    from db.users import UserOperations
    from db.agents import AgentOperations
    user_ops = UserOperations()
    agent_ops = AgentOperations(user_ops)
    return ScheduleOperations(user_ops, agent_ops)


def test_latest_execution_picks_newest_per_schedule(sched_ops):
    seed_schedule("s1")
    seed_schedule("s2")
    seed_execution("s1", started_at="2026-01-01T10:00:00.000000Z", status="success", exec_id="s1-old")
    seed_execution("s1", started_at="2026-01-02T10:00:00.000000Z", status="failed", exec_id="s1-new")
    seed_execution("s2", started_at="2026-01-01T09:00:00.000000Z", status="success", exec_id="s2-only")

    out = sched_ops.get_latest_execution_per_schedule(["s1", "s2"])

    assert set(out.keys()) == {"s1", "s2"}
    assert out["s1"]["id"] == "s1-new"          # newest started_at wins
    assert out["s1"]["status"] == "failed"
    assert out["s2"]["id"] == "s2-only"
    # slim projection: only dashboard fields, no large TEXT blobs (#1265 review fix)
    assert set(out["s1"]) == {"id", "status", "started_at", "completed_at", "duration_ms", "error"}


def test_latest_execution_empty_input_returns_empty(sched_ops):
    assert sched_ops.get_latest_execution_per_schedule([]) == {}


def test_latest_execution_schedule_without_runs_absent(sched_ops):
    seed_schedule("s1")
    seed_schedule("s2")
    seed_execution("s1", exec_id="s1-only")

    out = sched_ops.get_latest_execution_per_schedule(["s1", "s2"])

    assert "s1" in out
    assert "s2" not in out                   # no executions -> absent, not None entry


# ---------------------------------------------------------------------------
# get_latest_activity_for_agents
# ---------------------------------------------------------------------------

@pytest.fixture
def act_ops(db_backend):
    from db.activities import ActivityOperations
    return ActivityOperations()


def test_latest_activity_picks_newest_per_agent(act_ops):
    _seed_activity("a1-old", "agent-1", "2026-01-01T10:00:00.000000Z", state="completed")
    _seed_activity("a1-new", "agent-1", "2026-01-02T10:00:00.000000Z", state="started")
    _seed_activity("a2", "agent-2", "2026-01-01T08:00:00.000000Z", state="started")

    out = act_ops.get_latest_activity_for_agents(["agent-1", "agent-2"])

    assert out["agent-1"]["id"] == "a1-new"
    assert out["agent-1"]["activity_state"] == "started"
    assert out["agent-2"]["id"] == "a2"


def test_latest_activity_empty_input_returns_empty(act_ops):
    assert act_ops.get_latest_activity_for_agents([]) == {}


def test_latest_activity_unknown_agent_absent(act_ops):
    _seed_activity("a1", "agent-1", "2026-01-01T10:00:00.000000Z")
    out = act_ops.get_latest_activity_for_agents(["agent-1", "ghost"])
    assert "agent-1" in out and "ghost" not in out


# ---------------------------------------------------------------------------
# get_activities_in_range(agent_names=...) — timeline access filter
# ---------------------------------------------------------------------------

def test_timeline_agent_names_none_returns_all(act_ops):
    _seed_activity("a1", "agent-1", "2026-01-01T10:00:00.000000Z")
    _seed_activity("a2", "agent-2", "2026-01-01T11:00:00.000000Z")

    rows = act_ops.get_activities_in_range(agent_names=None)
    names = {r["agent_name"] for r in rows}
    assert names == {"agent-1", "agent-2"}


def test_timeline_agent_names_subset_filters_in_sql(act_ops):
    _seed_activity("a1", "agent-1", "2026-01-01T10:00:00.000000Z")
    _seed_activity("a2", "agent-2", "2026-01-01T11:00:00.000000Z")

    rows = act_ops.get_activities_in_range(agent_names=["agent-1"])
    assert {r["agent_name"] for r in rows} == {"agent-1"}


def test_timeline_agent_names_empty_returns_nothing(act_ops):
    _seed_activity("a1", "agent-1", "2026-01-01T10:00:00.000000Z")
    assert act_ops.get_activities_in_range(agent_names=[]) == []
