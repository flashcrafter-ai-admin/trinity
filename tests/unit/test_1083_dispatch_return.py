"""Unit tests for execute_task fire-and-forget dispatch-and-return (#1083, PR2.F).

Drives the real ``execute_task`` with a mocked agent transport and asserts:

- async-eligible trigger ({schedule,webhook}) + DISPATCH_ASYNC + a 202 ACK →
  returns RUNNING/dispatched_async in-line, sets the durable async marker, and
  does NOT release the slot (the callback owns the lease) — AC1.
- non-202 (200/old image/non-Claude) → today's synchronous handling + slot
  release (the non-202 fallback) — Finding 2.
- non-eligible trigger (loop/fan_out) under DISPATCH_ASYNC → stays synchronous,
  payload async_result=False, legacy 'dispatched' marker (T1/#4).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _resp(status_code, body=None):
    r = MagicMock()
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json.return_value = body if body is not None else {}
    return r


def _run(*, triggered_by, dispatch_async, agent_resp):
    """Drive execute_task with config.DISPATCH_ASYNC=dispatch_async and a mocked
    agent response. Returns (result, mocks dict)."""
    import config
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 300
    mock_db.get_execution.return_value = MagicMock(status="cancelled")
    mock_db.update_execution_status.return_value = True

    mock_capacity = MagicMock()
    admitted = MagicMock(state="admitted")
    mock_capacity.acquire = AsyncMock(return_value=admitted)
    mock_capacity.release = AsyncMock()

    mock_circuit = MagicMock()
    mock_circuit.allow_request.return_value = True

    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-1"),
        complete_activity=AsyncMock(),
    )
    post_mock = AsyncMock(return_value=agent_resp)

    with (
        patch.object(config, "DISPATCH_ASYNC", dispatch_async),
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry", post_mock),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
    ):
        svc = TaskExecutionService()
        result = _await(
            svc.execute_task(
                agent_name="test-agent",
                message="hi",
                triggered_by=triggered_by,
                execution_id="exec-1083",
                timeout_seconds=300,
                model="sonnet",
            )
        )
    return result, {"db": mock_db, "capacity": mock_capacity, "post": post_mock}


def _payload_of(post_mock):
    # agent_post_with_retry(agent, endpoint, payload, ...)
    return post_mock.await_args.args[2]


class TestAsyncDispatchReturn:
    def test_schedule_202_returns_running_no_slot_release(self):
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(
            triggered_by="schedule",
            dispatch_async=True,
            agent_resp=_resp(202, {"execution_id": "exec-1083", "status": "accepted"}),
        )
        assert result.status == TaskExecutionStatus.RUNNING
        assert result.dispatched_async is True
        # Slot lease handed to the callback — NOT released in finally (AC1).
        m["capacity"].release.assert_not_awaited()
        # Durable async marker written.
        m["db"].mark_execution_dispatched.assert_called_once_with("exec-1083", async_dispatch=True)
        # Payload requested async.
        assert _payload_of(m["post"])["async_result"] is True

    def test_webhook_is_also_eligible(self):
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(
            triggered_by="webhook",
            dispatch_async=True,
            agent_resp=_resp(202, {"status": "accepted"}),
        )
        assert result.status == TaskExecutionStatus.RUNNING
        assert result.dispatched_async is True

    def test_non_202_falls_back_to_sync(self):
        """An agent that returns 200 (old image / non-Claude) is handled
        synchronously and the slot IS released — the non-202 fallback."""
        from services.task_execution_service import TaskExecutionStatus

        body = {
            "response": "done",
            "session_id": "s1",
            "metadata": {"cost_usd": 0.01, "context_window": 200000},
            "execution_log": [],
        }
        result, m = _run(
            triggered_by="schedule", dispatch_async=True, agent_resp=_resp(200, body),
        )
        assert result.status == TaskExecutionStatus.SUCCESS
        assert result.dispatched_async is False
        m["capacity"].release.assert_awaited_once()  # sync path releases

    def test_flag_off_never_async(self):
        from services.task_execution_service import TaskExecutionStatus

        body = {"response": "ok", "metadata": {"context_window": 200000}, "execution_log": []}
        result, m = _run(
            triggered_by="schedule", dispatch_async=False, agent_resp=_resp(200, body),
        )
        assert result.status == TaskExecutionStatus.SUCCESS
        assert _payload_of(m["post"])["async_result"] is False
        m["db"].mark_execution_dispatched.assert_called_once_with("exec-1083", async_dispatch=False)


class TestTriggerScope:
    """T1/#4: loop/fan_out stay synchronous even with DISPATCH_ASYNC on."""

    @pytest.mark.parametrize("trigger", ["loop", "fan_out", "manual", "mcp", "public", "agent"])
    def test_non_eligible_trigger_stays_sync(self, trigger):
        from services.task_execution_service import TaskExecutionStatus

        body = {"response": "ok", "metadata": {"context_window": 200000}, "execution_log": []}
        result, m = _run(
            triggered_by=trigger, dispatch_async=True, agent_resp=_resp(200, body),
        )
        assert result.status == TaskExecutionStatus.SUCCESS
        assert _payload_of(m["post"])["async_result"] is False
        m["db"].mark_execution_dispatched.assert_called_once_with("exec-1083", async_dispatch=False)
        m["capacity"].release.assert_awaited_once()
