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


def _sealed_request(**overrides) -> ParallelTaskRequest:
    values = {
        "message": "run",
        "model": "claude-sonnet-4-6",
        "allowed_tools": ["Bash"],
        "system_prompt": "platform boundary",
        "timeout_seconds": 900,
        "max_turns": 2,
        "execution_id": "exec-sealed",
        "resume_session_id": None,
        "persist_session": False,
        "images": None,
        "operation_grant": _grant(),
        "async_result": False,
    }
    values.update(overrides)
    return ParallelTaskRequest(**values)


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


def test_sealed_setup_forces_empty_mcp_and_no_session_persistence(monkeypatch, tmp_path):
    """WHY: a long sealed turn must not inherit MCP tools or write resumable JSONL state."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    (tmp_path / ".mcp.json").write_text('{"mcpServers":{"danger":{}}}')

    context = _setup_headless_command(
        prompt="perform task",
        model="claude-sonnet-4-6",
        allowed_tools=["Bash"],
        system_prompt=None,
        timeout_seconds=3600,
        max_turns=12,
        execution_id="operation-test",
        resume_session_id=None,
        persist_session=False,
        images=None,
        operation_grant=_grant(),
    )

    assert "--no-session-persistence" in context.cmd
    assert "--strict-mcp-config" in context.cmd
    mcp_index = context.cmd.index("--mcp-config")
    assert context.cmd[mcp_index + 1] == '{"mcpServers":{}}'
    assert str(tmp_path / ".mcp.json") not in context.cmd


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
async def test_legacy_task_endpoint_rejects_sealed_grant():
    with pytest.raises(HTTPException, match="sealed task endpoint") as failure:
        await chat.execute_task(ParallelTaskRequest(message="run", operation_grant=_grant()))

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_sealed_task_endpoint_requires_grant():
    with pytest.raises(HTTPException, match="requires an operation grant") as failure:
        await chat.execute_sealed_task(ParallelTaskRequest(message="run"))

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_async_sealed_task_rejected_before_dispatch():
    with pytest.raises(HTTPException, match="invalid sealed task contract") as failure:
        await chat.execute_sealed_task(_sealed_request(async_result=True))

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_runtime_rejects_sealed_grant_before_execution(monkeypatch):
    runtime = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(sealed_operation_grant=False)
    )
    monkeypatch.setattr(chat.result_callback, "try_spawn_async", lambda _request: False)
    monkeypatch.setattr(chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(chat, "assert_sealed_runtime_eligible", lambda: None)

    with pytest.raises(HTTPException, match="does not support sealed operation grants") as failure:
        await chat.execute_sealed_task(_sealed_request())

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_exact_inner_sealed_wire_reaches_runtime_gate(monkeypatch):
    request = _sealed_request()
    assert request.operation_wire_exact is True

    async def accepted(_request, *, operation_grant):
        assert operation_grant == _grant()
        return {"accepted": True}

    monkeypatch.setattr(chat, "_execute_task", accepted)
    monkeypatch.setattr(chat, "assert_sealed_runtime_eligible", lambda: None)
    assert await chat.execute_sealed_task(request) == {"accepted": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"shadow_override": "discarded"},
        {"max_turns": "2"},
        {"max_turns": True},
        {"async_result": 0},
        {"persist_session": 0},
        {"timeout_seconds": "900"},
        {"operation_wire_exact": True},
    ],
)
async def test_inner_sealed_wire_rejects_coercion_and_extra_fields(
    monkeypatch, overrides
):
    request = _sealed_request(**overrides)
    assert request.operation_wire_exact is False

    async def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("non-exact sealed wire reached runtime dispatch")

    monkeypatch.setattr(chat, "_execute_task", unexpected_dispatch)
    with pytest.raises(HTTPException, match="invalid sealed task contract") as failure:
        await chat.execute_sealed_task(request)

    assert failure.value.status_code == 422


def test_generic_runtime_without_hardened_profile_is_ineligible(tmp_path):
    from agent_server.services.sealed_runtime import sealed_runtime_eligibility

    result = sealed_runtime_eligibility(profile_path=tmp_path / "missing.json")
    assert result.eligible is False
    assert result.reason == "profile"


def test_hardened_profile_requires_exact_root_owned_runtime_boundaries(monkeypatch, tmp_path):
    from agent_server.services import sealed_runtime

    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"schemaVersion":"trinity-sealed-runtime/1","developerUid":1000,'
        '"operationUid":1001,"sudoAuthorized":false,'
        '"managedBashHook":true,"strictMcp":true}'
    )
    profile.chmod(0o444)
    monkeypatch.setattr(sealed_runtime, "_REQUIRED_BOUNDARIES", ((profile, 0o444, False),))
    monkeypatch.setattr(sealed_runtime.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "getgid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "getegid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "getgroups", lambda: [])
    monkeypatch.setattr(sealed_runtime, "_effective_capabilities", lambda: 0)
    monkeypatch.setattr(sealed_runtime, "_sudo_authorized", lambda: False)

    assert sealed_runtime.sealed_runtime_eligibility(profile_path=profile).eligible is True

    profile.chmod(0o644)
    result = sealed_runtime.sealed_runtime_eligibility(profile_path=profile)
    assert result.eligible is False
    assert result.reason == "boundary"


def test_sealed_route_is_distinct_from_legacy_task_route():
    paths = {route.path for route in chat.router.routes}
    assert "/api/task" in paths
    assert "/api/task/sealed" in paths


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
    assert "operation_wire_exact" not in request.model_dump()
