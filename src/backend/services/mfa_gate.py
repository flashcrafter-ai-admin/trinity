"""MFA gate — the OSS seam the enterprise 2FA module (#5) plugs into.

The login path (``routers/auth.py``) must stay edition-agnostic: OSS-only
builds have no second factor and behave exactly as before. This module is the
single hook point. An enterprise module registers a *provider* at startup; the
auth routers call :func:`gate_login` after primary credentials are verified.

* OSS-only build → no provider registered → :func:`gate_login` returns ``None``
  → the router issues the JWT normally. Zero behavioural change.
* Enterprise build with 2FA entitled → the provider decides whether this user
  must complete a second factor; if so, :func:`gate_login` returns a *challenge
  response* (a short-lived challenge token + flags) and the router returns that
  instead of an access token. The frontend then completes the flow against
  ``/api/enterprise/2fa/login/*``, which mints the real access token.

The provider holds all the IP (TOTP verify, policy). This module only knows
the *protocol*:

    provider.gate_decision(user: dict) -> {"enrolled": bool, "required": bool}

``user`` is the OSS user row dict (``id``, ``username``, ``role``, ``email``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


class MfaProvider(Protocol):
    def gate_decision(self, user: dict) -> dict:  # {"enrolled": bool, "required": bool}
        ...


_provider: Optional[MfaProvider] = None


def register_provider(provider: MfaProvider) -> None:
    """Register the enterprise MFA provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[mfa_gate] provider registered: %s", type(provider).__name__)


def get_provider() -> Optional[MfaProvider]:
    return _provider


def clear_provider() -> None:
    """Drop the provider — used by tests to restore the OSS no-op path."""
    global _provider
    _provider = None


def gate_login(user: dict, mode: str) -> Optional[dict[str, Any]]:
    """Decide whether ``user`` must complete a second factor before a token
    is issued. Returns ``None`` to proceed normally, or a challenge response.

    Fail-open by design (consistent with Trinity's availability bias for
    cross-cutting gates): if the provider errors we log and let the password
    factor stand rather than locking everyone out. This is a deliberate
    tradeoff documented for #5 — a hard fail-closed would turn any 2FA bug
    into a full-platform login outage.
    """
    provider = _provider
    if provider is None:
        return None  # OSS-only build — no second factor
    try:
        decision = provider.gate_decision(user) or {}
    except Exception:  # noqa: BLE001 — never let a 2FA bug block all logins
        logger.exception("[mfa_gate] provider.gate_decision failed; failing open")
        return None

    enrolled = bool(decision.get("enrolled"))
    required = bool(decision.get("required"))
    if not enrolled and not required:
        return None  # user has no 2FA and policy doesn't force it

    # Late import: keeps this module importable without the full auth stack
    # (e.g. isolated unit tests of the registry).
    from dependencies import create_mfa_challenge_token

    challenge = create_mfa_challenge_token(user["username"], mode)
    return {
        "mfa_required": True,
        "mfa_enrolled": enrolled,
        "enrollment_required": required and not enrolled,
        "challenge_token": challenge,
    }
