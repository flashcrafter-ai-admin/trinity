"""Connector-scoped execution termination must stay bound to one agent."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _await(coro):
    return asyncio.run(coro)


def _row(agent_name: str, status: str):
    return SimpleNamespace(agent_name=agent_name, status=status)


def _connector(agent_name: str = "agent-1"):
    return SimpleNamespace(id=1, connector_agent=agent_name)


def _terminate(*, path_row, task_execution_id=None, task_row=None, mock_db=None):
    import routers.chat as chat

    mock_db = mock_db or MagicMock()

    def get_execution(execution_id):
        if execution_id == "path-exec":
            return path_row
        if execution_id == task_execution_id:
            return task_row
        return None

    mock_db.get_execution.side_effect = get_execution
    mock_db.cancel_queued_execution.return_value = True
    activity = MagicMock(track_activity=AsyncMock())

    with (
        patch.object(chat, "db", mock_db),
        patch.object(chat, "activity_service", activity),
        patch.object(chat, "get_agent_container") as get_container,
    ):
        result = _await(
            chat.terminate_agent_execution(
                execution_id="path-exec",
                task_execution_id=task_execution_id,
                name="agent-1",
                current_user=_connector(),
            )
        )
    return result, mock_db, get_container


@pytest.mark.parametrize("task_execution_id", [None, "path-exec"])
def test_connector_cancels_queued_execution_owned_by_bound_agent(task_execution_id):
    from models import TaskExecutionStatus

    result, mock_db, get_container = _terminate(
        path_row=_row("agent-1", TaskExecutionStatus.QUEUED),
        task_execution_id=task_execution_id,
    )

    assert result["status"] == "cancelled_while_queued"
    mock_db.cancel_queued_execution.assert_called_once_with(
        "path-exec", reason="Cancelled by user while queued"
    )
    get_container.assert_not_called()


def test_connector_rejects_queued_execution_owned_by_another_agent():
    from models import TaskExecutionStatus

    mock_db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _terminate(
            path_row=_row("agent-2", TaskExecutionStatus.QUEUED),
            mock_db=mock_db,
        )

    assert exc.value.status_code == 404
    mock_db.cancel_queued_execution.assert_not_called()
    mock_db.update_execution_status.assert_not_called()


def test_connector_rejects_foreign_task_execution_id_before_mutation():
    from models import TaskExecutionStatus

    mock_db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _terminate(
            path_row=_row("agent-1", TaskExecutionStatus.RUNNING),
            task_execution_id="foreign-exec",
            task_row=_row("agent-2", TaskExecutionStatus.QUEUED),
            mock_db=mock_db,
        )

    assert exc.value.status_code == 403
    mock_db.cancel_queued_execution.assert_not_called()
    mock_db.update_execution_status.assert_not_called()


def test_connector_rejects_mismatched_bound_task_execution_id():
    from models import TaskExecutionStatus

    mock_db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _terminate(
            path_row=_row("agent-1", TaskExecutionStatus.RUNNING),
            task_execution_id="different-bound-exec",
            task_row=_row("agent-1", TaskExecutionStatus.QUEUED),
            mock_db=mock_db,
        )

    assert exc.value.status_code == 403
    mock_db.cancel_queued_execution.assert_not_called()
    mock_db.update_execution_status.assert_not_called()


def test_non_connector_preserves_independent_task_execution_id_behavior():
    import routers.chat as chat
    from models import TaskExecutionStatus

    mock_db = MagicMock()
    mock_db.get_execution.return_value = _row("agent-2", TaskExecutionStatus.QUEUED)
    mock_db.cancel_queued_execution.return_value = True
    activity = MagicMock(track_activity=AsyncMock())
    user = SimpleNamespace(id=1, connector_agent=None)

    with (
        patch.object(chat, "db", mock_db),
        patch.object(chat, "activity_service", activity),
        patch.object(chat, "get_agent_container") as get_container,
    ):
        result = _await(
            chat.terminate_agent_execution(
                execution_id="path-exec",
                task_execution_id="legacy-independent-id",
                name="agent-1",
                current_user=user,
            )
        )

    assert result["status"] == "cancelled_while_queued"
    mock_db.cancel_queued_execution.assert_called_once_with(
        "legacy-independent-id", reason="Cancelled by user while queued"
    )
    get_container.assert_not_called()
