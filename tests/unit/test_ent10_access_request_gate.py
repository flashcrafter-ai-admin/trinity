"""Offline unit test for the public self-signup gate (trinity-enterprise#10).

Exercises routers.auth.request_access directly with a stubbed db + fake Request,
so it runs without a live backend. Asserts the secure default (403 when the
`public_access_requests_enabled` setting is off, with NO whitelist write) and
the operator-enabled path (auto-whitelist).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import routers.auth as auth  # noqa: E402


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.client = type("C", (), {"host": "1.2.3.4"})()

    async def json(self):
        return self._payload


class _FakeDb:
    def __init__(self, *, self_signup, whitelisted=False):
        self._self_signup = self_signup
        self._whitelisted = whitelisted
        self.added = []

    def get_setting_value(self, key, default=None):
        if key == "setup_completed":
            return "true"
        if key == "email_auth_enabled":
            return "true"
        if key == "public_access_requests_enabled":
            return "true" if self._self_signup else "false"
        return default

    def is_email_whitelisted(self, email):
        return self._whitelisted

    def add_to_whitelist(self, email, **kwargs):
        self.added.append((email, kwargs))


@pytest.fixture(autouse=True)
def _patch_auth(monkeypatch):
    # No-op the rate-limit side effects so the handler is pure-logic here.
    monkeypatch.setattr(auth, "check_login_rate_limit", lambda ip: None, raising=False)
    monkeypatch.setattr(auth, "record_login_attempt", lambda ip, success: None, raising=False)


def test_self_signup_disabled_returns_403_and_does_not_whitelist(monkeypatch):
    fake_db = _FakeDb(self_signup=False)
    monkeypatch.setattr(auth, "db", fake_db)

    with pytest.raises(auth.HTTPException) as exc:
        asyncio.run(auth.request_access(_FakeRequest({"email": "evil@example.com"})))

    assert exc.value.status_code == 403
    assert fake_db.added == [], "disabled self-signup must not whitelist anyone"


def test_self_signup_enabled_whitelists_new_email(monkeypatch):
    fake_db = _FakeDb(self_signup=True, whitelisted=False)
    monkeypatch.setattr(auth, "db", fake_db)

    result = asyncio.run(auth.request_access(_FakeRequest({"email": "Friend@Example.com"})))

    assert result["success"] is True
    assert result["already_registered"] is False
    assert "granted" not in result["message"].lower()  # wording no longer claims an admin decision
    assert fake_db.added and fake_db.added[0][0] == "friend@example.com"  # normalized
    assert fake_db.added[0][1].get("default_role") == "user"  # #314: never creator


def test_self_signup_enabled_idempotent_for_existing(monkeypatch):
    fake_db = _FakeDb(self_signup=True, whitelisted=True)
    monkeypatch.setattr(auth, "db", fake_db)

    result = asyncio.run(auth.request_access(_FakeRequest({"email": "known@example.com"})))

    assert result["already_registered"] is True
    assert fake_db.added == []  # already present → no re-add


def test_self_signup_enabled_rejects_invalid_email(monkeypatch):
    fake_db = _FakeDb(self_signup=True)
    monkeypatch.setattr(auth, "db", fake_db)

    with pytest.raises(auth.HTTPException) as exc:
        asyncio.run(auth.request_access(_FakeRequest({"email": "not-an-email"})))
    assert exc.value.status_code == 400
