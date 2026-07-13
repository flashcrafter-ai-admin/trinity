"""Raw Claude transcript compatibility for governed tool evidence."""

import sys
from pathlib import Path

_BASE_IMAGE = Path(__file__).resolve().parents[2] / "docker" / "base-image"
sys.path.insert(0, str(_BASE_IMAGE))

from agent_server.services.stream_parser import normalize_tool_result_status


def test_missing_tool_result_status_is_normalized_to_explicit_success():
    transcript = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "ok"},
                {"type": "tool_result", "tool_use_id": "bad", "is_error": True},
            ]
        },
    }

    assert normalize_tool_result_status(transcript) is transcript
    results = transcript["message"]["content"]
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True
