from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "src" / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

from services.task_execution_service import (  # noqa: E402
    TaskExecutionErrorCode,
    _provider_error_code_from_response,
)

pytestmark = pytest.mark.unit


def test_credit_balance_response_is_billing_failure():
    assert (
        _provider_error_code_from_response(
            "Credit balance is too low. Add credits to your Anthropic account."
        )
        == TaskExecutionErrorCode.BILLING
    )


def test_auth_response_is_auth_failure():
    assert (
        _provider_error_code_from_response(
            "Authentication failure: Not logged in. Please run /login."
        )
        == TaskExecutionErrorCode.AUTH
    )


def test_normal_agent_response_is_not_demoted():
    assert (
        _provider_error_code_from_response(
            "The client onboarding checklist is blocked because access is missing."
        )
        is None
    )
