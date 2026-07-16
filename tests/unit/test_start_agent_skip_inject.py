"""
Unit tests for #421: skip credential/skill injection when starting an
already-running agent container.

`start_agent_internal` previously called `inject_assigned_credentials`
and `inject_assigned_skills` unconditionally after `container_start`.
On an already-running, busy container this produced 3 connection retries
per call and an ERROR log even though the workspace volume already
carries `.env` and `.claude/skills/` across restarts.

Issue: https://github.com/abilityai/trinity/issues/421
Module: src/backend/services/agent_service/lifecycle.py
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

_BACKEND = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'src', 'backend'
))

# ── Shared mocks ──────────────────────────────────────────────────────────
_mock_db = MagicMock()
_mock_docker_service = MagicMock()
_mock_docker_utils = MagicMock()
_mock_settings = MagicMock()
_mock_skill_service = MagicMock()
_mock_helpers = MagicMock()
_mock_read_only = MagicMock()


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code = status_code
        self.detail = detail


_SYS_MOCKS = {
    'database': Mock(db=_mock_db),
    'docker': Mock(),
    'services.docker_service': Mock(
        docker_client=Mock(),
        get_agent_container=_mock_docker_service.get_agent_container,
    ),
    'services.docker_utils': Mock(
        container_stop=AsyncMock(),
        container_remove=AsyncMock(),
        container_start=_mock_docker_utils.container_start,
        container_reload=_mock_docker_utils.container_reload,
        volume_get=AsyncMock(),
        volume_create=AsyncMock(),
        containers_run=AsyncMock(),
    ),
    'services.settings_service': Mock(
        get_anthropic_api_key=Mock(return_value="sk-test"),
        get_github_pat=Mock(return_value=""),
        get_agent_full_capabilities=Mock(return_value=False),
    ),
    'services.skill_service': Mock(skill_service=_mock_skill_service),
    'fastapi': Mock(HTTPException=_HTTPException),
}

# Make the async docker utils actually awaitable
_mock_docker_utils.container_start = AsyncMock()
_mock_docker_utils.container_reload = AsyncMock()
_SYS_MOCKS['services.docker_utils'].container_start = _mock_docker_utils.container_start
_SYS_MOCKS['services.docker_utils'].container_reload = _mock_docker_utils.container_reload


# ── Load the module under test via importlib ──────────────────────────────
def _load_lifecycle():
    # Load the package stub first so relative imports (.helpers, .read_only)
    # resolve to mocks we control.
    pkg_name = "agent_service_pkg_under_test"
    pkg_spec = importlib.util.spec_from_loader(pkg_name, loader=None, is_package=True)
    pkg = importlib.util.module_from_spec(pkg_spec)
    pkg.__path__ = [os.path.join(_BACKEND, "services", "agent_service")]
    sys.modules[pkg_name] = pkg

    helpers_mod = Mock(
        check_shared_folder_mounts_match=AsyncMock(return_value=True),
        check_api_key_env_matches=Mock(return_value=True),
        check_github_pat_env_matches=Mock(return_value=True),
        check_resource_limits_match=Mock(return_value=True),
        check_full_capabilities_match=Mock(return_value=True),
        check_guardrails_env_matches=Mock(return_value=True),
        validate_base_image=Mock(),
    )
    read_only_mod = Mock(inject_read_only_hooks=AsyncMock(
        return_value={"success": True}
    ))
    file_sharing_mod = Mock(
        check_public_folder_mount_matches=Mock(return_value=True),
    )

    sys.modules[f"{pkg_name}.helpers"] = helpers_mod
    sys.modules[f"{pkg_name}.read_only"] = read_only_mod
    sys.modules[f"{pkg_name}.file_sharing"] = file_sharing_mod

    # Names that downstream tests import from `services.agent_service`. Adding
    # them here prevents this stub package — which can persist for the rest of
    # the pytest session — from contaminating later tests with ImportError.
    pkg.get_accessible_agents = Mock(return_value=[])
    pkg.get_agent_owner_id = Mock(return_value=1)
    pkg.list_agents_data = Mock(return_value=[])

    # Also register under the import path used by lifecycle.py
    sys.modules['services.agent_service'] = pkg
    sys.modules['services.agent_service.helpers'] = helpers_mod
    sys.modules['services.agent_service.read_only'] = read_only_mod
    sys.modules['services.agent_service.file_sharing'] = file_sharing_mod

    with patch.dict('sys.modules', _SYS_MOCKS):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.lifecycle",
            os.path.join(_BACKEND, "services", "agent_service", "lifecycle.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod, helpers_mod


_mod, _helpers = _load_lifecycle()


# ── Helpers ───────────────────────────────────────────────────────────────
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_container(status: str):
    c = Mock()
    c.status = status
    return c


@pytest.fixture(autouse=True)
def _reset():
    _mock_docker_service.reset_mock()
    _mock_docker_utils.container_start.reset_mock()
    _mock_docker_utils.container_reload.reset_mock()
    _mock_db.reset_mock()
    # Re-point get_agent_container at the name imported into lifecycle.
    _mod.get_agent_container = _mock_docker_service.get_agent_container
    # By default, no recreation needed. Patch the names bound into the
    # lifecycle module at import time.
    _mod.check_shared_folder_mounts_match = AsyncMock(return_value=True)
    _mod.check_api_key_env_matches = Mock(return_value=True)
    _mod.check_github_pat_env_matches = Mock(return_value=True)
    _mod.check_resource_limits_match = Mock(return_value=True)
    _mod.check_full_capabilities_match = Mock(return_value=True)
    _mod.check_guardrails_env_matches = Mock(return_value=True)
    _mod.platform_package_mounts_match = Mock(return_value=True)
    # By default, public folder mount matches the file_sharing_enabled flag
    _mod.check_public_folder_mount_matches = Mock(return_value=True)
    # By default, no read-only mode
    _mock_db.get_read_only_mode.return_value = {"enabled": False}


# ── Tests ─────────────────────────────────────────────────────────────────
class TestStartAgentSkipInject:
    pytestmark = pytest.mark.unit

    def test_skips_injection_when_container_already_running(self):
        """When container was running and no recreation needed, do not
        call credential or skill injection (#421)."""
        container = _make_container("running")
        _mock_docker_service.get_agent_container.return_value = container

        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})

        with patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills):
            result = _run(_mod.start_agent_internal("agent-a"))

        inject_creds.assert_not_awaited()
        inject_skills.assert_not_awaited()
        assert result["credentials_injection"] == "skipped"
        assert result["credentials_result"]["reason"] == "container_already_running"
        assert result["skills_injection"] == "skipped"
        assert result["skills_result"]["reason"] == "container_already_running"

    def test_injects_when_container_was_stopped(self):
        """When container was not running before start, inject as before."""
        container = _make_container("exited")
        _mock_docker_service.get_agent_container.return_value = container

        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})

        with patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills):
            result = _run(_mod.start_agent_internal("agent-b"))

        inject_creds.assert_awaited_once_with("agent-b")
        inject_skills.assert_awaited_once_with("agent-b")
        assert result["credentials_injection"] == "success"
        assert result["skills_injection"] == "success"

    def test_injects_when_container_recreated_even_if_was_running(self):
        """Recreation produces a fresh container; injection must still run."""
        container = _make_container("running")
        _mock_docker_service.get_agent_container.return_value = container
        # Force recreation via a helper that flips `needs_recreation` True.
        _mod.check_api_key_env_matches = Mock(return_value=False)

        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})
        recreate = AsyncMock()

        with patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills), \
             patch.object(_mod, "recreate_container_with_updated_config", recreate):
            _run(_mod.start_agent_internal("agent-c"))

        recreate.assert_awaited_once()
        inject_creds.assert_awaited_once_with("agent-c")
        inject_skills.assert_awaited_once_with("agent-c")
