"""The public sealed-executor wire is parsed before Pydantic can normalize or echo it."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


def _grant() -> str:
    return f"{'a' * 96}.{'b' * 96}"


def _wire(**overrides) -> bytes:
    value = {
        "message": "execute the signed operation",
        "allowed_tools": ["Bash"],
        "max_turns": 12,
        "async_mode": False,
        "operation_grant": _grant(),
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode()


def test_exact_raw_wire_parses_without_serializing_the_grant():
    from dependencies import parse_sealed_task_wire

    parsed = parse_sealed_task_wire(_wire())
    assert parsed.operation_wire_exact is True
    assert parsed.operation_grant.get_secret_value() == _grant()
    assert _grant() not in repr(parsed)
    assert _grant() not in parsed.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"message":"run","message":"shadow","allowed_tools":["Bash"],"max_turns":12,"async_mode":false,"operation_grant":"x"}',
        _wire(message=None),
        _wire(max_turns=True),
        _wire(max_turns="12"),
        _wire(async_mode=0),
        _wire(allowed_tools=["Bash", "Read"]),
        _wire(operation_grant="too-short"),
        _wire(extra="discard-me"),
    ],
)
def test_non_exact_raw_wire_fails_with_one_secret_free_error(body):
    from dependencies import parse_sealed_task_wire

    with pytest.raises(HTTPException) as failure:
        parse_sealed_task_wire(body)
    assert failure.value.status_code == 403
    assert failure.value.detail == "Sealed task contract rejected"
    assert _grant() not in str(failure.value)


def test_missing_required_field_never_echoes_the_raw_grant():
    from dependencies import parse_sealed_task_wire

    body = _wire()
    value = json.loads(body)
    del value["message"]
    raw = json.dumps(value).encode()
    with pytest.raises(HTTPException) as failure:
        parse_sealed_task_wire(raw)
    assert _grant() not in str(failure.value)


def test_sealed_principal_is_bound_to_one_endpoint_and_agent(monkeypatch):
    import dependencies as deps

    monkeypatch.setattr(
        deps.db,
        "validate_mcp_api_key",
        lambda *_a, **_k: {
            "scope": "sealed_executor",
            "agent_name": "agent-paid-media",
            "key_id": "sealed-key-1",
            "key_name": "sealed-agent-paid-media-key",
            "user_id": "owner",
            "user_email": "owner@example.com",
        },
    )
    monkeypatch.setattr(
        deps.db,
        "get_user_by_email",
        lambda *_a, **_k: {
            "id": 1,
            "username": "owner",
            "email": "owner@example.com",
            "role": "admin",
        },
    )

    def auth(method: str, path: str):
        request = SimpleNamespace(method=method, url=SimpleNamespace(path=path))
        return asyncio.run(deps.get_current_user(request, token="trinity_mcp_fake"))

    user = auth("POST", "/api/agents/agent-paid-media/task/sealed")
    assert user.sealed_executor_agent == "agent-paid-media"
    assert user.sealed_executor_key_id == "sealed-key-1"

    for method, path in [
        ("POST", "/api/agents/agent-paid-media/chat"),
        ("POST", "/api/agents/agent-paid-media/task"),
        ("POST", "/api/agents/agent-web/task/sealed"),
        ("GET", "/api/agents/agent-paid-media/connector/playbooks"),
    ]:
        with pytest.raises(HTTPException) as failure:
            auth(method, path)
        assert failure.value.status_code == 403


def test_sealed_scope_is_rejected_by_the_central_mcp_validation_surface(monkeypatch):
    from db.mcp_keys import scope_can_authenticate_to_mcp
    from routers import mcp_keys

    assert scope_can_authenticate_to_mcp("sealed_executor") is False
    for scope in ("user", "agent", "system", "connector"):
        assert scope_can_authenticate_to_mcp(scope) is True

    monkeypatch.setattr(
        mcp_keys.db,
        "validate_mcp_api_key",
        lambda *_a, **_k: {
            "scope": "sealed_executor",
            "agent_name": "agent-paid-media",
            "user_id": "owner",
        },
    )
    request = SimpleNamespace(
        headers={"Authorization": "Bearer trinity_mcp_runtime_only"}
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(mcp_keys.validate_mcp_api_key_http_endpoint(request))
    assert failure.value.status_code == 401


def test_sealed_endpoint_parses_before_runtime_lookup(monkeypatch):
    from models import User
    from routers import chat

    raw = MagicMock()
    raw.headers = {}
    raw.body = lambda: None
    user = User(
        id=1,
        username="owner",
        role="admin",
        sealed_executor_agent="agent-paid-media",
        sealed_executor_key_id="sealed-key-1",
    )

    async def body():
        return b'{"operation_grant":"secret"}'

    raw.body = body
    monkeypatch.setattr(
        chat,
        "get_agent_container",
        lambda _name: pytest.fail("malformed wire reached runtime lookup"),
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(
            chat.execute_sealed_parallel_task(
                raw_request=raw,
                name="agent-paid-media",
                current_user=user,
                idempotency_key="dispatch-1",
            )
        )
    assert failure.value.status_code == 403
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "name",
    ["x-source-agent", "x-via-mcp", "x-mcp-key-id", "x-mcp-key-name"],
)
def test_sealed_endpoint_rejects_caller_attribution_before_body_parse(name):
    from routers.chat import _reject_sealed_attribution_headers

    request = _stream_request([_wire()], [(name.encode(), b"")])
    with pytest.raises(HTTPException) as failure:
        _reject_sealed_attribution_headers(request)
    assert failure.value.status_code == 403
    assert failure.value.detail == "Sealed task contract rejected"


def test_sealed_endpoint_accepts_only_non_attribution_transport_headers():
    from routers.chat import _reject_sealed_attribution_headers

    request = _stream_request(
        [_wire()],
        [(b"content-type", b"application/json"), (b"idempotency-key", b"dispatch-1")],
    )
    _reject_sealed_attribution_headers(request)


def _stream_request(chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None):
    queue = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.request", "body": b""}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/agents/agent-paid-media/task/sealed",
            "headers": headers or [],
        },
        receive,
    )


def test_sealed_body_reader_accepts_exact_bounded_stream():
    from routers.chat import _read_sealed_task_body

    request = _stream_request([b"abc", b"def"], [(b"content-length", b"6")])
    assert asyncio.run(_read_sealed_task_body(request)) == b"abcdef"


@pytest.mark.parametrize(
    "raw_request",
    [
        _stream_request([b"x"], [(b"content-length", b"65537")]),
        _stream_request([b"x"], [(b"content-length", b"+1")]),
        _stream_request([b"x" * 40_000, b"y" * 30_000]),
    ],
)
def test_sealed_body_reader_rejects_declared_or_streamed_overflow(raw_request):
    from routers.chat import _read_sealed_task_body

    with pytest.raises(HTTPException) as failure:
        asyncio.run(_read_sealed_task_body(raw_request))
    assert failure.value.status_code == 403
    assert failure.value.detail == "Sealed task contract rejected"


def test_sealed_key_rotation_endpoint_returns_only_runtime_credential(monkeypatch):
    from models import User
    from routers import sealed_executor

    monkeypatch.setattr(
        sealed_executor.db,
        "regenerate_sealed_executor_key",
        lambda agent_name, user_id: {
            "key_id": "sealed-1",
            "api_key": "trinity_mcp_secret",
            "key_prefix": "trinity_mcp_secret"[:20],
        },
    )
    result = asyncio.run(
        sealed_executor.regenerate_sealed_executor_key(
            agent_name="agent-paid-media",
            current_user=User(id=7, username="owner", role="admin"),
        )
    )
    assert result.model_dump() == {
        "agent_name": "agent-paid-media",
        "key_id": "sealed-1",
        "api_key": "trinity_mcp_secret",
        "key_prefix": "trinity_mcp_secret"[:20],
    }
