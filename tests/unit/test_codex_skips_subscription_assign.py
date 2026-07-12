"""Non-Claude runtimes must not get a Claude subscription (#1187 decision 7).

Subscriptions are Claude-OAuth tokens (CLAUDE_CODE_OAUTH_TOKEN). Both the
create-time auto-assign (``crud.py``) and the recreate-time auth juggle
(``lifecycle.py``) gate on ``is_claude_runtime`` — the shared decision tested
here. If this returns True for ``codex``/``gemini`` a Codex agent would get a
persisted ``subscription_id`` (``has_subscription=True``) and a spurious Claude
token injected on every create/recreate.

The full create/recreate flow is exercised end-to-end in /verify-local; this
unit test pins the gating predicate that both call sites share.
"""

from __future__ import annotations

from services.agent_service.helpers import is_claude_runtime


def test_claude_variants_are_claude():
    assert is_claude_runtime("claude-code") is True
    assert is_claude_runtime("claude") is True
    # case-insensitive — labels/env may vary in case
    assert is_claude_runtime("Claude-Code") is True


def test_unset_or_empty_defaults_to_claude():
    """Back-compat: an unset/empty runtime is the Claude default, so existing
    agents keep their subscription behavior."""
    assert is_claude_runtime(None) is True
    assert is_claude_runtime("") is True


def test_codex_is_not_claude():
    """The load-bearing assertion: a Codex agent must NOT be assigned a Claude
    subscription / injected a Claude OAuth token."""
    assert is_claude_runtime("codex") is False


def test_gemini_is_not_claude():
    assert is_claude_runtime("gemini-cli") is False
    assert is_claude_runtime("gemini") is False
