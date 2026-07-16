"""Security contract for agent SSH host-port publication."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.docker_service import get_agent_ssh_port_binding  # noqa: E402


def test_agent_ssh_binding_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("AGENT_SSH_BIND_HOST", raising=False)
    assert get_agent_ssh_port_binding(2222) == ("127.0.0.1", 2222)


def test_agent_ssh_binding_accepts_explicit_ip(monkeypatch):
    monkeypatch.setenv("AGENT_SSH_BIND_HOST", "192.0.2.10")
    assert get_agent_ssh_port_binding(2223) == ("192.0.2.10", 2223)


def test_agent_ssh_binding_rejects_invalid_host(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_SSH_BIND_HOST", "0.0.0.0.example")
    with caplog.at_level(logging.WARNING, logger="services.docker_service"):
        binding = get_agent_ssh_port_binding(2224)
    assert binding == ("127.0.0.1", 2224)
    assert "Invalid AGENT_SSH_BIND_HOST" in caplog.text


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_agent_ssh_binding_rejects_wildcard_interfaces(monkeypatch, wildcard):
    monkeypatch.setenv("AGENT_SSH_BIND_HOST", wildcard)
    assert get_agent_ssh_port_binding(2225) == ("127.0.0.1", 2225)


def test_every_agent_creation_path_uses_secure_binding_helper():
    expected = {
        "src/backend/services/agent_service/crud.py": "get_agent_ssh_port_binding(config.port)",
        "src/backend/services/agent_service/lifecycle.py": "get_agent_ssh_port_binding(ssh_port)",
        "src/backend/services/system_agent_service.py": "get_agent_ssh_port_binding(ssh_port)",
    }
    for relative_path, call in expected.items():
        source = (_ROOT / relative_path).read_text()
        assert call in source, f"{relative_path} bypasses secure SSH host binding"
