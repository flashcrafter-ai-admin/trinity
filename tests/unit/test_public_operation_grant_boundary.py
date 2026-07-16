"""Public sealed grants fail closed before persistence or asynchronous dispatch."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from models import ParallelTaskRequest
from routers import chat


@pytest.mark.asyncio
async def test_async_sealed_grant_rejected_before_idempotency_or_persistence(monkeypatch):
    # WHY: a secret grant must never enter backlog/idempotency persistence.
    container = SimpleNamespace(status="running")
    begin_called = False

    def begin(*_args, **_kwargs):
        nonlocal begin_called
        begin_called = True
        raise AssertionError("idempotency must not run")

    monkeypatch.setattr(chat, "get_agent_container", lambda _name: container)
    monkeypatch.setattr(chat.idempotency_service, "begin", begin)
    request = ParallelTaskRequest(
        message="run",
        async_mode=True,
        operation_grant=f"{'a' * 96}.{'b' * 96}",
    )
    user = SimpleNamespace(
        id=1,
        email=None,
        username="operator",
        agent_name=None,
        connector_agent=None,
    )

    with pytest.raises(HTTPException, match="require synchronous tasks") as failure:
        await chat.execute_parallel_task(
            request=request,
            name="agent-onboarding",
            current_user=user,
            x_source_agent=None,
            x_via_mcp=None,
            x_mcp_key_id=None,
            x_mcp_key_name=None,
            idempotency_key="must-not-persist",
        )

    assert failure.value.status_code == 422
    assert begin_called is False
