"""
Connector-scope auth boundary (OSS edition-agnostic primitive, ent#46).

The connector *feature* (config, key minting, snippets, UI) is an entitled
module in the private repo. What ships in OSS core is the security enforcement:
core recognizes a `scope='connector'` MCP key as a consumption-only principal
and fences it to its bound agent, refusing owner and role-gated operations —
the same core-primitive + enterprise-knob shape as `users.suspended_at` (#995).

These tests pin that enforcement.
"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_user, seed_agent  # noqa: E402,F401

pytestmark = pytest.mark.unit


def _user(
    connector_agent=None,
    sealed_executor_agent=None,
    sealed_executor_key_id=None,
    role="creator",
):
    from models import User
    return User(
        id=1,
        username="owner",
        role=role,
        connector_agent=connector_agent,
        sealed_executor_agent=sealed_executor_agent,
        sealed_executor_key_id=sealed_executor_key_id,
    )


def _fake_request(method, path):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


class TestConnectorScope:
    def test_non_connector_is_noop(self):
        from dependencies import _enforce_connector_scope
        # Ordinary principal: never fenced, even on an owner op.
        _enforce_connector_scope(_user(), "agent-1", owner_op=True)

    def test_connector_blocked_on_owner_op(self):
        from fastapi import HTTPException
        from dependencies import _enforce_connector_scope
        with pytest.raises(HTTPException) as exc:
            _enforce_connector_scope(_user(connector_agent="agent-1"), "agent-1", owner_op=True)
        assert exc.value.status_code == 403

    def test_connector_fenced_to_bound_agent(self):
        from fastapi import HTTPException
        from dependencies import _enforce_connector_scope
        # A different agent is refused...
        with pytest.raises(HTTPException) as exc:
            _enforce_connector_scope(_user(connector_agent="agent-1"), "agent-2", owner_op=False)
        assert exc.value.status_code == 403
        # ...the bound agent is allowed.
        _enforce_connector_scope(_user(connector_agent="agent-1"), "agent-1", owner_op=False)


class TestRoleGate:
    def test_connector_rejected_from_role_gate(self):
        from fastapi import HTTPException
        from dependencies import _reject_connector_principal
        # Even resolving to an admin owner, a connector key can't role-gate.
        with pytest.raises(HTTPException) as exc:
            _reject_connector_principal(_user(connector_agent="agent-1", role="admin"))
        assert exc.value.status_code == 403

    def test_ordinary_principal_passes(self):
        from dependencies import _reject_connector_principal
        _reject_connector_principal(_user())


class TestUserModel:
    def test_connector_agent_defaults_none(self):
        from models import User
        u = User(id=1, username="owner", role="user")
        assert u.connector_agent is None


class TestCentralGuard:
    """get_current_user is the single auth entry point — a connector key must be
    contained there to the exact routes its MCP tools call, so the ~dozens of
    endpoints with inline access checks can't be reached by a leaked snippet."""

    @pytest.fixture
    def patched(self, monkeypatch):
        import dependencies as deps
        monkeypatch.setattr(deps.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": "connector", "agent_name": "agent-1",
            "user_id": "owner", "user_email": "owner@example.com",
        })
        monkeypatch.setattr(deps.db, "get_user_by_email", lambda *_a, **_k: {
            "id": 1, "username": "owner", "email": "owner@example.com", "role": "creator",
        })
        return deps

    def _call(self, deps, request):
        # A non-JWT token falls through to the MCP-key path.
        return asyncio.run(deps.get_current_user(request, token="trinity_mcp_fake"))

    def test_allows_bound_agent_chat(self, patched):
        u = self._call(patched, _fake_request("POST", "/api/agents/agent-1/chat"))
        assert u.connector_agent == "agent-1"

    def test_allows_bound_agent_playbooks(self, patched):
        u = self._call(patched, _fake_request("GET", "/api/agents/agent-1/connector/playbooks"))
        assert u.connector_agent == "agent-1"

    def test_blocks_bound_agent_task_route(self, patched):
        """WHY: an end-user connector bearer must never enter executable task dispatch."""
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("POST", "/api/agents/agent-1/task"))
        assert exc.value.status_code == 403

    def test_blocks_other_agent_chat(self, patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("POST", "/api/agents/agent-2/chat"))
        assert exc.value.status_code == 403

    def test_blocks_other_agent_task(self, patched):
        """WHY: widening the task path must not widen the connector's agent identity."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("POST", "/api/agents/agent-2/task"))
        assert exc.value.status_code == 403

    def test_blocks_owner_endpoint_on_bound_agent(self, patched):
        from fastapi import HTTPException
        # An inline-checked endpoint on the bound agent (e.g. loops) is refused
        # at the auth layer before any handler runs.
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("POST", "/api/agents/agent-1/loops"))
        assert exc.value.status_code == 403

    def test_blocks_wrong_method(self, patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("DELETE", "/api/agents/agent-1/chat"))
        assert exc.value.status_code == 403

    @pytest.mark.parametrize(
        ("scope", "agent_name"),
        [
            ("connector", None),
            ("connector", ""),
            ("connector", "Agent With Spaces"),
            ("agent", None),
            ("future-scope", "agent-1"),
        ],
    )
    def test_rejects_unknown_or_unbound_scope_before_owner_resolution(
        self, monkeypatch, scope, agent_name
    ):
        import dependencies as deps

        owner_lookup = MagicMock(return_value={
            "id": 1,
            "username": "owner",
            "email": "owner@example.com",
            "role": "admin",
        })
        monkeypatch.setattr(
            deps.db,
            "validate_mcp_api_key",
            lambda *_a, **_k: {
                "scope": scope,
                "agent_name": agent_name,
                "user_id": "owner",
                "user_email": "owner@example.com",
            },
        )
        monkeypatch.setattr(deps.db, "get_user_by_email", owner_lookup)

        with pytest.raises(HTTPException) as exc:
            self._call(deps, _fake_request("DELETE", "/api/agents/other"))

        assert exc.value.status_code == 401
        owner_lookup.assert_not_called()


class TestSealedExecutorTaskContract:
    """Only a sealed-executor bearer authorizes stateless operation execution.

    The backend pins the transport shape; the root-owned agent runtime verifies
    the grant signature and claims before exposing an operation command.
    """

    @staticmethod
    def _grant():
        return f"{'a' * 96}.{'b' * 96}"

    @staticmethod
    def _request(**overrides):
        from models import ParallelTaskRequest
        values = {
            "message": "execute the signed operation",
            "allowed_tools": ["Bash"],
            "max_turns": 12,
            "async_mode": False,
            "operation_grant": TestSealedExecutorTaskContract._grant(),
        }
        values.update(overrides)
        request = ParallelTaskRequest(**values)
        if request.operation_wire_exact is True:
            request.operation_request_digest = hashlib.sha256(
                b"exact sealed test request"
            ).hexdigest()
        return request

    @staticmethod
    def _enforce(request, **overrides):
        from dependencies import _enforce_sealed_executor_task_request
        arguments = {
            "idempotency_key": "dispatch:task:attempt:1",
            "x_source_agent": None,
            "x_via_mcp": None,
            "x_mcp_key_id": None,
            "x_mcp_key_name": None,
        }
        arguments.update(overrides)
        _enforce_sealed_executor_task_request(
            _user(
                sealed_executor_agent="agent-1",
                sealed_executor_key_id="sealed-key-1",
            ),
            request,
            **arguments,
        )

    def test_accepts_exact_sync_sealed_task(self):
        """WHY: the runtime principal gets one narrowly shaped signed task call."""
        request = self._request()
        assert request.operation_wire_exact is True
        self._enforce(request)

    def test_wire_parser_binds_request_to_exact_raw_bytes(self):
        from dependencies import parse_sealed_task_wire

        body = json.dumps(
            {
                "allowed_tools": ["Bash"],
                "async_mode": False,
                "max_turns": 12,
                "message": "execute the signed operation",
                "operation_grant": self._grant(),
            },
            separators=(",", ":"),
        ).encode()
        request = parse_sealed_task_wire(body)
        assert request.operation_request_digest == hashlib.sha256(body).hexdigest()
        assert "operation_request_digest" not in request.model_dump()

    def test_sealed_idempotency_claim_replays_only_exact_request_bytes(self):
        from services import idempotency_service

        scope = idempotency_service.make_sealed_agent_scope("agent-idem-exact")
        key = "dispatch:task:attempt:exact"
        first = idempotency_service.begin(scope, key, request_digest="a" * 64)
        assert first.enabled is True
        assert first.replay is False

        exact_replay = idempotency_service.begin(
            scope, key, request_digest="a" * 64
        )
        assert exact_replay.replay is True
        assert exact_replay.in_flight is True
        assert exact_replay.conflict is False

        changed_request = idempotency_service.begin(
            scope, key, request_digest="b" * 64
        )
        assert changed_request.replay is True
        assert changed_request.conflict is True

    def test_sealed_contract_rejects_a_missing_request_digest(self):
        request = self._request()
        request.operation_request_digest = None
        with pytest.raises(HTTPException) as exc:
            self._enforce(request)
        assert exc.value.status_code == 403

    def test_task_route_checks_sealed_contract_before_container_lookup(self, monkeypatch):
        """WHY: the body gate precedes runtime state, idempotency, and execution."""
        from fastapi import HTTPException
        from routers import chat

        def unexpected_lookup(_name):
            pytest.fail("sealed contract did not fail before agent lookup")

        monkeypatch.setattr(chat, "get_agent_container", unexpected_lookup)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(chat.execute_parallel_task(
                request=self._request(operation_grant=None),
                name="agent-1",
                current_user=_user(
                    sealed_executor_agent="agent-1",
                    sealed_executor_key_id="sealed-key-1",
                ),
                x_source_agent=None,
                x_via_mcp=None,
                x_mcp_key_id=None,
                x_mcp_key_name=None,
                idempotency_key="dispatch:task:attempt:1",
            ))
        assert exc.value.status_code == 403

    def test_task_route_accepts_contract_before_normal_agent_lookup(self, monkeypatch):
        """WHY: a valid sealed request must enter the existing task path unchanged."""
        from fastapi import HTTPException
        from routers import chat

        monkeypatch.setattr(chat, "get_agent_container", lambda _name: None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(chat.execute_parallel_task(
                request=self._request(),
                name="agent-1",
                current_user=_user(
                    sealed_executor_agent="agent-1",
                    sealed_executor_key_id="sealed-key-1",
                ),
                x_source_agent=None,
                x_via_mcp=None,
                x_mcp_key_id=None,
                x_mcp_key_name=None,
                idempotency_key="dispatch:task:attempt:1",
            ))
        assert exc.value.status_code == 404

    def test_valid_sealed_task_threads_turn_cap_to_execution_service(self, monkeypatch):
        """WHY: validating a cap is insufficient unless the agent receives it."""
        from models import TaskExecutionStatus
        from routers import chat

        container = SimpleNamespace(status="running")
        decision = SimpleNamespace(
            enabled=True,
            replay=False,
            in_flight=False,
            execution_id=None,
            snapshot=None,
        )
        db = MagicMock()
        db.get_agent_subscription_id.return_value = None
        db.get_max_parallel_tasks.return_value = 3
        db.get_execution_timeout.return_value = 900
        db.create_task_execution.return_value = SimpleNamespace(id="exec-sealed")
        capacity = MagicMock()
        capacity.acquire = AsyncMock(return_value=SimpleNamespace(state="admitted"))
        service = MagicMock()
        service.execute_task = AsyncMock(
            return_value=SimpleNamespace(
                status=TaskExecutionStatus.SUCCESS,
                response="done",
                error=None,
                raw_response={"response": "done"},
            )
        )

        monkeypatch.setattr(chat, "get_agent_container", lambda _name: container)
        monkeypatch.setattr(chat, "db", db)
        monkeypatch.setattr(chat, "get_capacity_manager", lambda: capacity)
        monkeypatch.setattr(chat, "dispatch_breaker_active", lambda _name: False)
        monkeypatch.setattr(chat, "get_task_execution_service", lambda: service)
        monkeypatch.setattr(
            chat.idempotency_service, "begin", lambda *_a, **_kw: decision
        )
        monkeypatch.setattr(chat.idempotency_service, "attach_execution", lambda *_a: None)
        monkeypatch.setattr(chat.idempotency_service, "complete", lambda *_a: None)

        response = asyncio.run(
            chat.execute_parallel_task(
                request=self._request(max_turns=12),
                name="agent-1",
                current_user=_user(
                    sealed_executor_agent="agent-1",
                    sealed_executor_key_id="sealed-key-1",
                ),
                x_source_agent=None,
                x_via_mcp=None,
                x_mcp_key_id=None,
                x_mcp_key_name=None,
                idempotency_key="dispatch:task:attempt:1",
            )
        )

        assert response["task_execution_id"] == "exec-sealed"
        kwargs = service.execute_task.await_args.kwargs
        assert kwargs["max_turns"] == 12
        assert kwargs["allowed_tools"] == ["Bash"]
        assert kwargs["operation_grant"] == self._grant()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"shadow_override": "discarded-by-default-pydantic"},
            {"async_mode": 0},
            {"max_turns": "12"},
            {"operation_wire_exact": True},
        ],
    )
    def test_rejects_non_exact_sealed_wire_before_normalization(self, overrides):
        request = self._request(**overrides)
        assert request.operation_grant is not None
        assert request.operation_wire_exact is False
        with pytest.raises(HTTPException) as exc:
            self._enforce(request)
        assert exc.value.status_code == 403
        assert self._grant() not in str(exc.value)

    def test_sealed_task_fails_closed_when_idempotency_claim_is_unavailable(self, monkeypatch):
        """WHY: a sealed operation cannot run unless its replay claim exists."""
        from routers import chat

        container = SimpleNamespace(status="running")
        disabled = SimpleNamespace(
            enabled=False,
            replay=False,
            in_flight=False,
            execution_id=None,
            snapshot=None,
        )
        db = MagicMock()
        db.get_agent_subscription_id.return_value = None
        db.get_max_parallel_tasks.return_value = 3
        db.get_execution_timeout.return_value = 900

        monkeypatch.setattr(chat, "get_agent_container", lambda _name: container)
        monkeypatch.setattr(chat, "db", db)
        monkeypatch.setattr(
            chat.idempotency_service, "begin", lambda *_a, **_kw: disabled
        )

        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                chat.execute_parallel_task(
                    request=self._request(),
                    name="agent-1",
                    current_user=_user(
                        sealed_executor_agent="agent-1",
                        sealed_executor_key_id="sealed-key-1",
                    ),
                    x_source_agent=None,
                    x_via_mcp=None,
                    x_mcp_key_id=None,
                    x_mcp_key_name=None,
                    idempotency_key="dispatch:task:attempt:1",
                )
            )

        assert exc.value.status_code == 503
        db.create_task_execution.assert_not_called()

    def test_sealed_task_rejects_key_reuse_with_changed_request_bytes(self, monkeypatch):
        """WHY: a caller key can replay only the exact authenticated wire."""
        from routers import chat

        conflict = SimpleNamespace(
            enabled=True,
            replay=True,
            in_flight=False,
            conflict=True,
            execution_id="exec-original",
            snapshot=None,
        )
        monkeypatch.setattr(
            chat,
            "get_agent_container",
            lambda _name: SimpleNamespace(status="running"),
        )
        monkeypatch.setattr(
            chat.idempotency_service, "begin", lambda *_a, **_kw: conflict
        )
        monkeypatch.setattr(chat.platform_audit_service, "log", AsyncMock())

        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                chat.execute_parallel_task(
                    request=self._request(),
                    name="agent-1",
                    current_user=_user(
                        sealed_executor_agent="agent-1",
                        sealed_executor_key_id="sealed-key-1",
                    ),
                    x_source_agent=None,
                    x_via_mcp=None,
                    x_mcp_key_id=None,
                    x_mcp_key_name=None,
                    idempotency_key="dispatch:task:attempt:1",
                )
            )

        assert exc.value.status_code == 409
        assert exc.value.detail["error"] == "idempotency_key_conflict"

    def test_non_sealed_principal_preserves_existing_task_contract(self):
        """WHY: sealed execution must not change ordinary user or agent task calls."""
        from dependencies import _enforce_sealed_executor_task_request
        _enforce_sealed_executor_task_request(
            _user(),
            self._request(operation_grant=None, allowed_tools=None, max_turns=None),
            idempotency_key=None,
            x_source_agent="agent-1",
            x_via_mcp="true",
            x_mcp_key_id="key-1",
            x_mcp_key_name="key",
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"operation_grant": None},
            {"operation_grant": "too-short"},
            {"operation_grant": f" {'a' * 96}.{'b' * 96}"},
            {"operation_grant": f"{'a' * 96}.{'b' * 96}\n"},
            {"operation_grant": f"{'a' * 96}.{'b' * 96}.extra"},
            {"operation_grant": f"{'a' * 96}.{'b' * 95}!"},
            {"allowed_tools": None},
            {"allowed_tools": ["Read"]},
            {"allowed_tools": ["Bash", "Read"]},
            {"async_mode": True},
            {"async_mode": None},
            {"max_turns": None},
            {"max_turns": 0},
            {"max_turns": 33},
            {"model": "some-model"},
            {"system_prompt": "override"},
            {"timeout_seconds": 1},
            {"files": []},
            {"save_to_session": True},
            {"save_to_session": None},
            {"create_new_session": True},
            {"create_new_session": None},
            {"chat_session_id": "session-1"},
            {"resume_session_id": "session-1"},
            {"inject_result": True},
            {"inject_result": None},
            {"user_message": "shadow message"},
        ],
    )
    def test_rejects_unsealed_or_broader_task_shapes(self, overrides):
        """WHY: connector execution must remain stateless and sealed to one command boundary."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._enforce(self._request(**overrides))
        assert exc.value.status_code == 403
        assert self._grant() not in str(exc.value.detail)

    @pytest.mark.parametrize(
        "headers",
        [
            {"idempotency_key": None},
            {"idempotency_key": " "},
            {"idempotency_key": " dispatch:task:attempt:1"},
            {"idempotency_key": "dispatch:task:attempt:1\n"},
            {"idempotency_key": "x" * 513},
            {"x_source_agent": "agent-1"},
            {"x_via_mcp": "true"},
            {"x_mcp_key_id": "spoofed"},
            {"x_mcp_key_name": "spoofed"},
        ],
    )
    def test_rejects_missing_idempotency_or_spoofable_source_headers(self, headers):
        """WHY: connector retries and audit attribution must be deterministic and non-spoofable."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._enforce(self._request(), **headers)
        assert exc.value.status_code == 403


class TestKeyCleanup:
    """ent#46 re-split fix: connector keys (scope='connector' in the OSS
    mcp_api_keys table) must be swept on agent delete, like scope='agent' keys."""

    def test_connector_key_in_agent_refs(self):
        from db.agent_cleanup import AGENT_REFS
        conn_refs = [r for r in AGENT_REFS
                     if r.table == "mcp_api_keys" and (r.extra_filter or "").find("connector") >= 0]
        assert len(conn_refs) == 1, "expected exactly one scope='connector' mcp_api_keys cleanup ref"

    def test_sealed_executor_key_in_agent_refs(self):
        from db.agent_cleanup import AGENT_REFS
        refs = [
            ref
            for ref in AGENT_REFS
            if ref.table == "mcp_api_keys"
            and "sealed_executor" in (ref.extra_filter or "")
        ]
        assert len(refs) == 1

    def test_cascade_delete_removes_connector_key(self, db_backend, monkeypatch):
        # Evict cached db modules so they bind to the harness backend.
        for mod in ("db.connection", "database"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        seed_user(user_id=1, username="owner")
        seed_agent(agent_name="agent-1", owner_id=1)
        from db.engine import get_engine
        from db.tables import mcp_api_keys
        from sqlalchemy import insert, select
        from db.agent_cleanup import cascade_delete
        with get_engine().begin() as conn:
            conn.execute(insert(mcp_api_keys).values(
                id="k1", name="connector-agent-1-key", key_prefix="trinity_mcp_xxxx",
                key_hash="hash-connector", created_at="2026-01-01T00:00:00Z",
                user_id=1, agent_name="agent-1", scope="connector", is_active=1,
            ))
        with get_engine().begin() as conn:
            cascade_delete(conn, "agent-1")
        with get_engine().connect() as conn:
            rows = conn.execute(select(mcp_api_keys.c.id).where(
                mcp_api_keys.c.agent_name == "agent-1")).all()
        assert rows == [], "connector key should be deleted with its agent"


class TestExtraAgentTableSeam:
    """The OSS rename/delete seam for entitled-module agent-scoped tables
    (register_agent_owned_table) — resolved by NAME via raw SQL so a private
    enterprise table can't KeyError the OSS metadata lookup."""

    def test_register_is_idempotent(self):
        from db.agent_cleanup import register_agent_owned_table, EXTRA_AGENT_REFS
        register_agent_owned_table("ent_demo_tbl", "agent_name")
        register_agent_owned_table("ent_demo_tbl", "agent_name")
        assert EXTRA_AGENT_REFS.count(("ent_demo_tbl", "agent_name")) == 1

    def test_cascade_delete_sweeps_registered_table(self, db_backend, monkeypatch):
        for mod in ("db.connection", "database"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        from db.engine import get_engine
        from sqlalchemy import text
        from db.agent_cleanup import register_agent_owned_table, cascade_delete
        with get_engine().begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS ent_demo_tbl (agent_name TEXT)"))
            conn.execute(text("INSERT INTO ent_demo_tbl (agent_name) VALUES ('a1')"))
        register_agent_owned_table("ent_demo_tbl", "agent_name")
        with get_engine().begin() as conn:
            cascade_delete(conn, "a1")
        with get_engine().connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ent_demo_tbl WHERE agent_name='a1'")).scalar()
        assert n == 0

    def test_cascade_delete_skips_absent_registered_table(self, db_backend, monkeypatch):
        # A registered table that doesn't exist (OSS-only build) must be skipped,
        # never raise.
        for mod in ("db.connection", "database"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        from db.engine import get_engine
        from db.agent_cleanup import register_agent_owned_table, cascade_delete
        register_agent_owned_table("ent_absent_tbl", "agent_name")
        with get_engine().begin() as conn:
            cascade_delete(conn, "whatever")  # no raise
