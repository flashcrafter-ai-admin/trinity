"""Cross-runtime and cross-database contract for sealed-task request digests."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
_MIGRATION = (
    _BACKEND
    / "migrations"
    / "versions"
    / "0019_idempotency_request_digest.py"
)


def test_database_facade_annotation_resolves_on_all_supported_python_versions():
    """Deferred annotations must not hide a missing runtime typing import."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_BACKEND), env.get("PYTHONPATH")) if part
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import typing; "
                "from database import DatabaseManager; "
                "assert typing.get_type_hints(DatabaseManager.idempotency_claim)"
                "['request_digest'] == typing.Optional[str]"
            ),
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_postgres_migration_extends_the_current_head_and_is_reversible(monkeypatch):
    execute = MagicMock()
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = types.SimpleNamespace(execute=execute)
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)

    spec = importlib.util.spec_from_file_location(
        "migration_0019_idempotency_request_digest", _MIGRATION
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "0019_idempotency_request_digest"
    assert migration.down_revision == "0018_schedule_executions_source_channel"

    migration.upgrade()
    execute.assert_called_once_with(
        "ALTER TABLE idempotency_keys "
        "ADD COLUMN IF NOT EXISTS request_digest TEXT"
    )

    execute.reset_mock()
    migration.downgrade()
    execute.assert_called_once_with(
        "ALTER TABLE idempotency_keys "
        "DROP COLUMN IF EXISTS request_digest"
    )
