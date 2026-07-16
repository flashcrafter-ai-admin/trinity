"""
#946 (T5): the /task dispatch-breaker deny path must release the idempotency
claim, mirroring /chat (routers/chat.py:242) and the /task CapacityFull branch
(1577/1694).

Before the fix, the async (1590) and sync (1706) CircuitOpen handlers called
``_raise_circuit_open_503`` WITHOUT ``idempotency_service.fail(idem)`` — so a
breaker-open deny left the ``in_flight`` idempotency row in place and blocked
every same-key retry for 24h (silent block). The #946 pilot routes agent→agent
sequential chat through this exact path, so it would exercise the bug.

These tests pin the release. They are unit tests over the real
``execute_parallel_task`` with its collaborators mocked — same style as
``test_chat_admission.py``.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from routers.chat import execute_parallel_task
from models import ParallelTaskRequest
from services.capacity_manager import CircuitOpen, CapacityFull

_CHAT = sys.modules[execute_parallel_task.__module__]


def _user(*, sealed=False):
    u = MagicMock()
    u.id = 1
    u.email = "u@e.com"
    u.username = "u"
    u.agent_name = None
    u.connector_agent = None
    u.sealed_executor_agent = "agent1" if sealed else None
    u.sealed_executor_key_id = "sealed-key-1" if sealed else None
    return u


def _idem(replay=False, in_flight=False, execution_id="e1"):
    m = MagicMock()
    m.replay = replay
    m.in_flight = in_flight
    m.execution_id = execution_id
    m.snapshot = None
    return m


@contextmanager
def _env(idem, acquire_exc):
    container = MagicMock()
    container.status = "running"

    isvc = MagicMock()
    isvc.begin.return_value = idem
    isvc.make_agent_scope.return_value = "agent:agent1"

    cap = MagicMock()
    cap.acquire = AsyncMock(side_effect=acquire_exc)

    db = MagicMock()
    db.get_execution_timeout.return_value = 3600
    db.get_max_parallel_tasks.return_value = 3
    db.get_agent_subscription_id.return_value = None
    db.create_task_execution.return_value = MagicMock(id="e1")

    with patch.object(_CHAT, "get_agent_container", return_value=container), \
         patch.object(_CHAT, "idempotency_service", isvc), \
         patch.object(_CHAT, "dispatch_breaker_active", return_value=True), \
         patch.object(_CHAT, "get_capacity_manager", return_value=cap), \
         patch.object(_CHAT, "platform_audit_service", MagicMock(log=AsyncMock())), \
         patch.object(_CHAT, "activity_service",
                      MagicMock(track_activity=AsyncMock(return_value="act1"))), \
         patch.object(_CHAT, "db", db):
        yield {"isvc": isvc, "cap": cap, "db": db}


def _call(async_mode, operation_grant=None):
    sealed = operation_grant is not None
    request = ParallelTaskRequest(
        message="hi",
        allowed_tools=["Bash"] if sealed else None,
        max_turns=12 if sealed else None,
        async_mode=async_mode,
        operation_grant=operation_grant,
    )
    if sealed:
        request.operation_request_digest = "a" * 64
    return asyncio.run(execute_parallel_task(
        request=request,
        name="agent1",
        current_user=_user(sealed=sealed),
        x_source_agent=None,
        x_via_mcp=None,
        x_mcp_key_id=None,
        x_mcp_key_name=None,
        idempotency_key="k1",
    ))


def test_async_breaker_open_releases_idem_and_503():
    with _env(_idem(), CircuitOpen("agent1", 5)) as m:
        with pytest.raises(HTTPException) as exc:
            _call(async_mode=True)
    assert exc.value.status_code == 503
    # The claim must be released so the same-key retry isn't blocked for 24h.
    m["isvc"].fail.assert_called_once()


def test_sync_breaker_open_releases_idem_and_503():
    with _env(_idem(), CircuitOpen("agent1", 5)) as m:
        with pytest.raises(HTTPException) as exc:
            _call(async_mode=False)
    assert exc.value.status_code == 503
    m["isvc"].fail.assert_called_once()


def test_async_capacity_full_still_releases_idem_and_429():
    """Regression guard: the CapacityFull branch already released the claim;
    keep it pinned alongside the CircuitOpen fix."""
    full = CapacityFull(agent_name="agent1", max_concurrent=3, reason="persistent_full", depth=50)
    with _env(_idem(), full) as m:
        with pytest.raises(HTTPException) as exc:
            _call(async_mode=True)
    assert exc.value.status_code == 429
    m["isvc"].fail.assert_called_once()


def test_sync_sealed_grant_never_enters_persistent_overflow():
    """WHY: secret capability bytes must not be serialized into the backlog."""
    full = CapacityFull(agent_name="agent1", max_concurrent=3, reason="rejected", depth=0)
    grant = f"{'a' * 96}.{'b' * 96}"
    with _env(_idem(), full) as m:
        with pytest.raises(HTTPException) as exc:
            _call(async_mode=False, operation_grant=grant)

    assert exc.value.status_code == 429
    kwargs = m["cap"].acquire.await_args.kwargs
    assert kwargs["overflow_policy"] == "reject"
    assert kwargs["overflow_payload"] is None
