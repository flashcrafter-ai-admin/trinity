import hashlib
import os
import stat

import pytest
from fastapi import HTTPException

from agent_server.models import CredentialInjectRequest
from agent_server.routers import credentials


@pytest.mark.asyncio
async def test_codex_auth_injection_enforces_runner_boundary_and_status(tmp_path, monkeypatch):
    subscription = tmp_path / ".codex-subscription"
    subscription.mkdir(mode=0o755)
    monkeypatch.setattr(credentials, "_HOME", tmp_path)
    payload = (
        '{"OPENAI_API_KEY":null,"auth_mode":"chatgpt",'
        '"tokens":{"access_token":"test","refresh_token":"refresh"}}\n'
    )

    response = await credentials.inject_credential_files(
        CredentialInjectRequest(files={".codex-subscription/auth.json": payload})
    )

    auth = subscription / "auth.json"
    assert response.files_written == [".codex-subscription/auth.json"]
    assert auth.read_text() == payload
    assert stat.S_IMODE(subscription.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600

    status = await credentials.get_credentials_status()
    projected = status["files"][".codex-subscription/auth.json"]
    assert projected["exists"] is True
    assert projected["sha256"] == hashlib.sha256(payload.encode()).hexdigest()
    assert projected["mode"] == "0600"
    assert projected["nlink"] == 1
    assert projected["parent_mode"] == "0700"


@pytest.mark.asyncio
async def test_codex_auth_injection_rejects_a_workspace_symlink(tmp_path, monkeypatch):
    subscription = tmp_path / ".codex-subscription"
    subscription.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("preserve\n")
    (subscription / "auth.json").symlink_to(victim)
    monkeypatch.setattr(credentials, "_HOME", tmp_path)

    with pytest.raises(HTTPException) as failure:
        await credentials.inject_credential_files(
            CredentialInjectRequest(files={".codex-subscription/auth.json": "replace\n"})
        )

    assert failure.value.status_code == 400
    assert victim.read_text() == "preserve\n"


@pytest.mark.asyncio
async def test_codex_auth_injection_rejects_a_parent_symlink(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".codex-subscription").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(credentials, "_HOME", tmp_path)

    with pytest.raises(HTTPException) as failure:
        await credentials.inject_credential_files(
            CredentialInjectRequest(files={
                ".codex-subscription/auth.json": (
                    '{"OPENAI_API_KEY":null,"auth_mode":"chatgpt",'
                    '"tokens":{"access_token":"test","refresh_token":"refresh"}}'
                )
            })
        )

    assert failure.value.status_code == 400
    assert not (outside / "auth.json").exists()


@pytest.mark.asyncio
async def test_codex_auth_injection_rejects_a_hard_link_before_truncation(tmp_path, monkeypatch):
    subscription = tmp_path / ".codex-subscription"
    subscription.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("preserve\n")
    os.link(victim, subscription / "auth.json")
    monkeypatch.setattr(credentials, "_HOME", tmp_path)

    with pytest.raises(HTTPException) as failure:
        await credentials.inject_credential_files(
            CredentialInjectRequest(files={
                ".codex-subscription/auth.json": (
                    '{"OPENAI_API_KEY":null,"auth_mode":"chatgpt",'
                    '"tokens":{"access_token":"test","refresh_token":"refresh"}}'
                )
            })
        )

    assert failure.value.status_code == 400
    assert victim.read_text() == "preserve\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        (
            '{"OPENAI_API_KEY":"metered-key","auth_mode":"chatgpt",'
            '"tokens":{"access_token":"test","refresh_token":"refresh"}}'
        ),
        (
            '{"OPENAI_API_KEY":"","auth_mode":"chatgpt",'
            '"tokens":{"access_token":"test","refresh_token":"refresh"}}'
        ),
        (
            '{"OPENAI_API_KEY":null,"auth_mode":"chatgpt","auth_mode":"apikey",'
            '"tokens":{"access_token":"test","refresh_token":"refresh"}}'
        ),
        (
            '{"OPENAI_API_KEY":null,"auth_mode":"chatgpt",'
            '"tokens":{"access_token":"test"}}'
        ),
    ],
)
async def test_codex_auth_injection_rejects_ambiguous_metered_or_incomplete_auth(
    tmp_path, monkeypatch, invalid
):
    monkeypatch.setattr(credentials, "_HOME", tmp_path)

    with pytest.raises(HTTPException) as failure:
        await credentials.inject_credential_files(
            CredentialInjectRequest(files={".codex-subscription/auth.json": invalid})
        )

    assert failure.value.status_code == 400
    assert not (tmp_path / ".codex-subscription" / "auth.json").exists()
