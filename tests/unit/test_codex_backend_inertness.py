"""Backend inertness for Codex-shaped failures (#1187 Phase H / decision 3).

Two backend behaviors must NOT misfire for Codex:

  1. The #678 stdout reader-race auto-retry must stay inert. It fires only on a
     Claude-specific 502 dict body (recovery_attempted + raw_message_count==0 +
     parse_failure_count==0 + num_turns<5 + "result message"). A Codex 502
     (early child exit) carries a plain string detail, so the signature check
     must return False — no spurious same-execution_id retry.

  2. A generic Codex failure must surface as 500, never 503. The backend infers
     error_code=AUTH only from a 503, and the dispatch breaker counts AUTH only,
     so a 500 keeps Codex failures out of the AUTH path. (The 500 mapping itself
     is asserted in test_codex_runtime.test_codex_generic_failure_not_auth.)
"""

from __future__ import annotations

from services.task_execution_service import _is_reader_race_signature


def test_reader_race_inert_for_codex_pipe_drop_string_detail():
    # Codex 502 detail is a plain string, not the reader-race dict.
    assert _is_reader_race_signature(
        "Agent subprocess closed before task could complete"
    ) is False


def test_reader_race_inert_for_codex_dict_without_recovery_marker():
    # Even a dict-shaped Codex error lacks recovery_attempted / the zeroed
    # counters, so it can never trip the Claude reader-race retry.
    codex_like = {
        "message": "Codex execution failed (exit code 1): boom",
        "raw_message_count": 4,
        "parse_failure_count": 0,
    }
    assert _is_reader_race_signature(codex_like) is False


def test_reader_race_still_fires_for_genuine_claude_signature():
    """Guard the guard: the real Claude reader-race body still matches, so the
    inertness above is specificity, not a broken predicate."""
    claude_sig = {
        "recovery_attempted": True,
        "raw_message_count": 0,
        "parse_failure_count": 0,
        "metadata": {"num_turns": 1},
        "message": "No result message received from Claude Code",
    }
    assert _is_reader_race_signature(claude_sig) is True
