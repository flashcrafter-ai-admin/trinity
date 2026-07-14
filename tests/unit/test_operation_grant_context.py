"""Sealed operation grants stay outside model-visible task surfaces."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_server.models import ParallelTaskRequest
from agent_server.routers import chat
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _headless_stdin_payload,
    _setup_headless_command,
)


def _grant() -> str:
    return f"{'a' * 96}.{'b' * 96}"


def test_request_masks_operation_grant_in_repr_and_json():
    grant = _grant()
    request = ParallelTaskRequest(message="run", operation_grant=grant)

    assert request.operation_grant is not None
    assert request.operation_grant.get_secret_value() == grant
    assert grant not in repr(request)
    assert grant not in request.model_dump_json()


def test_setup_wraps_claude_without_putting_grant_in_argv():
    grant = _grant()
    context = _setup_headless_command(
        prompt="perform task",
        model="claude-sonnet-4-6",
        allowed_tools=["Bash"],
        system_prompt=None,
        timeout_seconds=30,
        max_turns=2,
        execution_id="operation-test",
        resume_session_id=None,
        persist_session=False,
        images=None,
        operation_grant=grant,
    )

    assert context.cmd[:2] == [
        "/usr/local/bin/trinity-task-context",
        "/usr/local/bin/claude",
    ]
    assert all(grant not in argument for argument in context.cmd)
    assert context.operation_grant == grant


def test_stdin_prefixes_grant_once_before_the_model_prompt():
    grant = _grant()
    context = HeadlessRunContext(
        cmd=["/usr/local/bin/claude"],
        task_session_id="operation-test",
        task_start_iso="2026-07-13T00:00:00Z",
        effective_timeout=30,
        images=None,
        prompt="perform task",
        operation_grant=grant,
    )

    assert _headless_stdin_payload(context) == f"{grant}\nperform task"


@pytest.mark.asyncio
async def test_async_task_rejects_sealed_grant_before_dispatch():
    with pytest.raises(HTTPException, match="require synchronous tasks") as failure:
        await chat.execute_task(
            ParallelTaskRequest(message="run", operation_grant=_grant(), async_result=True)
        )

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_runtime_rejects_sealed_grant_before_execution(monkeypatch):
    runtime = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(sealed_operation_grant=False)
    )
    monkeypatch.setattr(chat.result_callback, "try_spawn_async", lambda _request: False)
    monkeypatch.setattr(chat, "get_runtime", lambda: runtime)

    with pytest.raises(HTTPException, match="does not support sealed operation grants") as failure:
        await chat.execute_task(ParallelTaskRequest(message="run", operation_grant=_grant()))

    assert failure.value.status_code == 422


def test_native_runner_is_fixed_to_claude_and_root_context_paths():
    source = (
        Path(__file__).parents[2] / "docker/base-image/task-context-runner.c"
    ).read_text()

    assert '"/usr/local/bin/claude"' in source
    assert 'CONTEXT_ROOT "/run/trinity-task-context"' in source
    assert "setsid()" in source
    assert '"session-start"' in source
    assert '"/proc/%ld/stat"' in source
    assert "PR_SET_PDEATHSIG" in source
    assert "O_NOFOLLOW" in source
    assert "setresuid(1000, 1000, 1000)" in source


def test_public_request_masks_and_excludes_operation_grant():
    from models import ParallelTaskRequest as PublicParallelTaskRequest

    grant = _grant()
    request = PublicParallelTaskRequest(message="run", operation_grant=grant)

    assert request.operation_grant is not None
    assert request.operation_grant.get_secret_value() == grant
    assert grant not in repr(request)
    assert "operation_grant" not in request.model_dump()
