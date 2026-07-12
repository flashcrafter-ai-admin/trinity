"""Migration runner atomicity + cross-process lock tests (#1160).

Two correctness defects in ``src/backend/db/migrations.py``:

1. DROP-rebuild data-loss window — the two table-rebuild migrations held the
   only copy of ``agent_sharing`` / ``agent_skills`` rows in a Python list
   between an autocommitted ``DROP TABLE`` and the re-INSERT commit. A crash in
   that window lost access-control rows permanently. Fixed by ``_atomic_rebuild``
   (rename-swap inside an explicit transaction — an explicit ``BEGIN`` holds DDL
   in a unit of work that rolls back cleanly under Python's legacy isolation).

2. No cross-process lock — the full suite ran concurrently from two uvicorn
   workers + the scheduler container. Fixed by ``db.migration_lock.migration_lock``
   (an OS flock on a sidecar file) wrapped around the whole runner.

These are pure unit tests — no backend or Docker. Multiprocessing (not threads)
is used for the cross-process cases because flock is per-open-file-description and
threads in one process would not exercise it.
"""

from __future__ import annotations

import importlib.util
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_MIGRATIONS_PATH = str(_BACKEND / "db" / "migrations.py")
_SCHEMA_PATH = str(_BACKEND / "db" / "schema.py")
_LOCK_PATH = str(_BACKEND / "db" / "migration_lock.py")


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_migrations = _load(_MIGRATIONS_PATH, "_migrations_1160")
_lock = _load(_LOCK_PATH, "_migration_lock_1160")

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class _CrashAfter:
    """Cursor proxy that executes a statement normally, then raises if it
    matches ``trigger`` (whitespace-normalised exact match). Simulates a process
    crash at a precise point inside a migration."""

    def __init__(self, real: sqlite3.Cursor, trigger: str) -> None:
        self._real = real
        self._trigger = trigger

    def execute(self, sql: str, *args, **kwargs):
        result = self._real.execute(sql, *args, **kwargs)
        if " ".join(sql.split()) == self._trigger:
            raise sqlite3.OperationalError(f"simulated crash after: {self._trigger}")
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def _seed_old_sharing(cur: sqlite3.Cursor) -> None:
    cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    cur.executemany(
        "INSERT INTO users VALUES (?, ?)",
        [(1, "Alice@Example.com"), (2, None), (3, "bob@example.com"), (4, "")],
    )
    cur.execute(
        """
        CREATE TABLE agent_sharing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            shared_with_id INTEGER NOT NULL,
            shared_by_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.executemany(
        "INSERT INTO agent_sharing (agent_name, shared_with_id, shared_by_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        [("a", 1, 9, "t"), ("a", 2, 9, "t"), ("a", 3, 9, "t"), ("a", 4, 9, "t")],
    )


def _seed_old_skills(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE agent_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            assigned_by TEXT NOT NULL,
            assigned_at TEXT NOT NULL
        )
        """
    )
    cur.executemany(
        "INSERT INTO agent_skills (agent_name, skill_id, assigned_by, assigned_at) "
        "VALUES (?, ?, ?, ?)",
        [("a", "writer", "u", "t"), ("a", "researcher", "u", "t")],
    )


# --------------------------------------------------------------------------- #
# Rename-swap happy path + behavior preservation
# --------------------------------------------------------------------------- #

def test_agent_sharing_rebuild_migrates_and_preserves_behavior(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    _seed_old_sharing(cur)
    conn.commit()

    _migrations._migrate_agent_sharing_table(cur, conn)

    cur.execute("PRAGMA table_info(agent_sharing)")
    cols = {r[1] for r in cur.fetchall()}
    assert "shared_with_email" in cols and "shared_with_id" not in cols

    cur.execute("SELECT agent_name, shared_with_email FROM agent_sharing ORDER BY shared_with_email")
    rows = cur.fetchall()
    # NULL email (id 2) and empty email (id 4) dropped; remaining lowercased —
    # matches the old `if share[2]:` Python filter + `.lower()`.
    assert rows == [("a", "alice@example.com"), ("a", "bob@example.com")]
    conn.close()


def test_agent_skills_rebuild_migrates_and_recreates_indexes(tmp_path):
    db = tmp_path / "k.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    _seed_old_skills(cur)
    conn.commit()

    _migrations._migrate_agent_skills_table(cur, conn)

    cur.execute("PRAGMA table_info(agent_skills)")
    cols = {r[1] for r in cur.fetchall()}
    assert "skill_name" in cols and "skill_id" not in cols

    cur.execute("SELECT agent_name, skill_name FROM agent_skills ORDER BY skill_name")
    assert cur.fetchall() == [("a", "researcher"), ("a", "writer")]

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    idx = {r[0] for r in cur.fetchall()}
    assert "idx_agent_skills_agent" in idx and "idx_agent_skills_skill" in idx
    conn.close()


def test_rebuild_is_noop_on_already_migrated_schema(tmp_path):
    """Second run (new schema already present) must do nothing and not raise —
    the cold-start re-run path."""
    db = tmp_path / "noop.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    _seed_old_sharing(cur)
    conn.commit()
    _migrations._migrate_agent_sharing_table(cur, conn)
    before = cur.execute("SELECT COUNT(*) FROM agent_sharing").fetchone()[0]

    _migrations._migrate_agent_sharing_table(cur, conn)  # no-op
    after = cur.execute("SELECT COUNT(*) FROM agent_sharing").fetchone()[0]
    assert before == after
    conn.close()


# --------------------------------------------------------------------------- #
# Atomicity: crash mid-rebuild must not lose data (the core defect)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "trigger",
    [
        "DROP TABLE agent_sharing",                       # crash right after the drop
        "ALTER TABLE agent_sharing_new RENAME TO agent_sharing",  # crash at the swap
    ],
)
def test_crash_during_rebuild_rolls_back_no_data_loss(tmp_path, trigger):
    db = tmp_path / "crash.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    _seed_old_sharing(cur)
    conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        _migrations._migrate_agent_sharing_table(_CrashAfter(cur, trigger), conn)

    # Old table and ALL rows survive — the data-loss window is closed.
    cur.execute("PRAGMA table_info(agent_sharing)")
    cols = {r[1] for r in cur.fetchall()}
    assert "shared_with_id" in cols, "old table was destroyed — data loss!"
    assert cur.execute("SELECT COUNT(*) FROM agent_sharing").fetchone()[0] == 4
    conn.close()


def test_replay_after_partial_apply_completes_cleanly(tmp_path):
    """Crash mid-migration, then replay the full runner — must finish with zero
    data loss (AC #3)."""
    db = tmp_path / "replay.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    _seed_old_sharing(cur)
    conn.commit()

    # Partial apply: crash after the DROP.
    with pytest.raises(sqlite3.OperationalError):
        _migrations._migrate_agent_sharing_table(
            _CrashAfter(cur, "DROP TABLE agent_sharing"), conn
        )
    # An orphaned agent_sharing_new must NOT have been left committed.
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_sharing_new'")
    assert cur.fetchone() is None

    # Replay: now it completes.
    _migrations._migrate_agent_sharing_table(cur, conn)
    cur.execute("SELECT agent_name, shared_with_email FROM agent_sharing ORDER BY shared_with_email")
    assert cur.fetchall() == [("a", "alice@example.com"), ("a", "bob@example.com")]
    conn.close()


# --------------------------------------------------------------------------- #
# Failing migration is named (AC #4)
# --------------------------------------------------------------------------- #

def test_run_all_migrations_names_failing_migration(tmp_path):
    db = tmp_path / "fail.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()

    def _boom(cursor, conn):
        raise ValueError("kaboom")

    original = _migrations.MIGRATIONS
    _migrations.MIGRATIONS = [("explosive_migration", _boom)]
    try:
        # Original type is preserved; the migration name rides the traceback note.
        with pytest.raises(ValueError) as exc_info:
            _migrations.run_all_migrations(cur, conn)
        assert any("explosive_migration" in n for n in getattr(exc_info.value, "__notes__", []))
    finally:
        _migrations.MIGRATIONS = original
    conn.close()


def test_migration_health_names_first_pending(tmp_path):
    """The /health helper reports applied/expected and names the first un-applied
    migration (drives the 503 body, #1160)."""
    db = tmp_path / "health.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    conn.commit()

    original = _migrations.MIGRATIONS
    _migrations.MIGRATIONS = [("m_a", lambda c, x: None),
                              ("m_b", lambda c, x: None),
                              ("m_c", lambda c, x: None)]
    try:
        # Nothing applied yet → first registered migration is pending.
        applied, expected, first_pending = _migrations.migration_health(cur)
        assert (applied, expected, first_pending) == (0, 3, "m_a")

        # Record the first; the gap moves to the next registered one.
        cur.execute("INSERT INTO schema_migrations VALUES ('m_a', 't')")
        conn.commit()
        applied, expected, first_pending = _migrations.migration_health(cur)
        assert (applied, expected, first_pending) == (1, 3, "m_b")

        # All applied → healthy, no pending migration.
        cur.executemany("INSERT INTO schema_migrations VALUES (?, 't')", [("m_b",), ("m_c",)])
        conn.commit()
        applied, expected, first_pending = _migrations.migration_health(cur)
        assert (applied, expected, first_pending) == (3, 3, None)
    finally:
        _migrations.MIGRATIONS = original
    conn.close()


# --------------------------------------------------------------------------- #
# Cross-process lock (multiprocessing — flock is per-process)
# --------------------------------------------------------------------------- #

def _lock_worker(lock_module_path: str, db_path: str, log_path: str, hold_s: float):
    """Subprocess: hold the migration lock briefly, recording enter/exit so the
    parent can prove critical sections never overlap."""
    spec = importlib.util.spec_from_file_location("ml_worker", lock_module_path)
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    with ml.migration_lock(db_path):
        with open(log_path, "a") as f:
            f.write(f"ENTER {os.getpid()}\n")
            f.flush()
        time.sleep(hold_s)
        with open(log_path, "a") as f:
            f.write(f"EXIT {os.getpid()}\n")
            f.flush()


def test_migration_lock_serialises_across_processes(tmp_path):
    db = tmp_path / "lock.db"
    db.write_text("")  # so the sidecar path's dir exists
    log = tmp_path / "order.log"
    log.write_text("")

    ctx = mp.get_context("spawn")  # portable (macOS dev + Linux CI)
    procs = [
        ctx.Process(target=_lock_worker, args=(_LOCK_PATH, str(db), str(log), 0.3))
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    events = [ln.split()[0] for ln in log.read_text().splitlines() if ln.strip()]
    assert len(events) == 8, f"expected 4 ENTER/EXIT pairs, got {events}"
    # Strict ENTER,EXIT,ENTER,EXIT,... nesting proves no two critical sections overlapped.
    assert events == ["ENTER", "EXIT"] * 4, f"lock allowed overlap: {events}"


def _boot_worker(migrations_path: str, lock_module_path: str, db_path: str):
    """Subprocess: mirror init_database's locked runner against a shared DB."""
    mig = _load(migrations_path, "mig_boot")
    spec = importlib.util.spec_from_file_location("ml_boot", lock_module_path)
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    with ml.migration_lock(db_path):
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        cur = conn.cursor()
        mig.run_all_migrations(cur, conn)
        conn.close()


def test_concurrent_boot_rebuild_is_safe(tmp_path):
    """Multiple processes boot at once against a DB needing the rebuild
    migration. The flock must let exactly one run it; the rest skip via
    schema_migrations — none crash, data intact (AC #1)."""
    db = tmp_path / "boot.db"
    seed = sqlite3.connect(str(db))
    scur = seed.cursor()
    _seed_old_sharing(scur)
    _seed_old_skills(scur)
    seed.commit()
    seed.close()

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_boot_worker, args=(_MIGRATIONS_PATH, _LOCK_PATH, str(db)))
        for _ in range(5)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, "a concurrent boot worker crashed"

    final = sqlite3.connect(str(db))
    fcur = final.cursor()
    fcur.execute("PRAGMA table_info(agent_sharing)")
    assert "shared_with_email" in {r[1] for r in fcur.fetchall()}
    fcur.execute("SELECT agent_name, shared_with_email FROM agent_sharing ORDER BY shared_with_email")
    assert fcur.fetchall() == [("a", "alice@example.com"), ("a", "bob@example.com")]
    fcur.execute("PRAGMA table_info(agent_skills)")
    assert "skill_name" in {r[1] for r in fcur.fetchall()}
    # Recorded once in the tracking table.
    fcur.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='agent_sharing'")
    assert fcur.fetchone()[0] == 1
    final.close()
