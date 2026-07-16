from pathlib import Path
import subprocess
import sys

import pytest


VALIDATOR = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "base-image"
    / "validate-codex-auth.py"
)
VALID = (
    b'{"OPENAI_API_KEY":null,"auth_mode":"chatgpt",'
    b'"tokens":{"access_token":"access","refresh_token":"refresh"}}\n'
)


def validate(payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(VALIDATOR)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_codex_subscription_validator_accepts_only_chatgpt_auth():
    result = validate(VALID)
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.parametrize(
    "payload",
    [
        b'{"OPENAI_API_KEY":"metered","auth_mode":"chatgpt","tokens":{}}',
        b'{"OPENAI_API_KEY":null,"auth_mode":"apikey","tokens":{}}',
        b'{"OPENAI_API_KEY":null,"auth_mode":"chatgpt","tokens":{"access_token":"a"}}',
        (
            b'{"OPENAI_API_KEY":null,"auth_mode":"chatgpt","auth_mode":"apikey",'
            b'"tokens":{"access_token":"a","refresh_token":"r"}}'
        ),
        b'{"OPENAI_API_KEY":null,"auth_mode":"chatgpt","tokens":{},"config":{}}',
    ],
)
def test_codex_subscription_validator_rejects_metered_ambiguous_or_incomplete_auth(payload):
    result = validate(payload)
    assert result.returncode != 0
    assert b"invalid Codex ChatGPT subscription auth" in result.stderr
    assert b"metered" not in result.stderr
