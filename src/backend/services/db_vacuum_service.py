"""
SQLite VACUUM Retention Service (Issue #772).

Daily APScheduler job that runs a full ``VACUUM`` on the main SQLite
database when the retention sweeps in ``cleanup_service`` have plausibly
freed enough space to be worth the cost.

VACUUM rewrites the entire database file under an exclusive lock, so it
deliberately runs once per day off-peak (04:30 UTC, 15 min after the
audit-log retention job, to spread the nightly writes). Per-cycle
``PRAGMA wal_checkpoint(TRUNCATE)`` in ``cleanup_service`` handles the
common case of freeing WAL pages back to the OS; VACUUM is the last-mile
reclaim that returns table pages.

Configuration (env vars):

- ``DB_VACUUM_ENABLED`` (default ``true``) — set ``false`` to disable.
- ``DB_VACUUM_HOUR`` (default ``4``) — UTC hour.
- ``DB_VACUUM_MINUTE`` (default ``30``) — UTC minute.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from db.connection import DB_PATH, connect_sqlite
from services.deployment_lock_service import governed_background_mutation

logger = logging.getLogger(__name__)


DB_VACUUM_ENABLED = os.getenv("DB_VACUUM_ENABLED", "true").lower() == "true"
DB_VACUUM_HOUR = int(os.getenv("DB_VACUUM_HOUR", "4"))
DB_VACUUM_MINUTE = int(os.getenv("DB_VACUUM_MINUTE", "30"))


class DBVacuumService:
    """Daily VACUUM of the main SQLite database."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        if not DB_VACUUM_ENABLED:
            logger.info("DB vacuum disabled (DB_VACUUM_ENABLED=false)")
            return

        # SQLite-only: VACUUM reclaims pages from the SQLite file. PostgreSQL
        # (#300) manages reclamation via autovacuum — this service is a no-op.
        from db.engine import is_sqlite
        if not is_sqlite():
            logger.info("DB vacuum skipped (PostgreSQL backend manages autovacuum)")
            return

        self.scheduler.add_job(
            self.vacuum_governed,
            CronTrigger(hour=DB_VACUUM_HOUR, minute=DB_VACUUM_MINUTE),
            id="db_vacuum",
            name="Daily SQLite VACUUM",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        logger.info(
            "DB vacuum scheduler started: daily at %02d:%02d UTC",
            DB_VACUUM_HOUR,
            DB_VACUUM_MINUTE,
        )

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("DB vacuum scheduler stopped")

    async def vacuum(self) -> Dict[str, Any]:
        """Run a single VACUUM. Returns summary for tests/manual triggers.

        VACUUM cannot run inside a transaction; we open a dedicated
        autocommit connection (``isolation_level=None``) for this reason.
        It also requires an exclusive lock, so we accept the rare BUSY
        outcome rather than retry — the next nightly run will catch up.
        """
        size_before = self._db_size_bytes()

        def _vacuum() -> None:
            conn = connect_sqlite(
                DB_PATH,
                timeout=300.0,
                isolation_level=None,
            )
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()

        try:
            from services.deployment_lock_service import run_governed_executor

            await run_governed_executor(None, _vacuum)
        except sqlite3.OperationalError as exc:
            logger.warning("VACUUM skipped: %s", exc)
            return {"status": "skipped", "reason": str(exc)}
        size_after = self._db_size_bytes()
        reclaimed = size_before - size_after
        logger.info(
            "VACUUM complete: size_before=%s size_after=%s reclaimed=%s",
            size_before,
            size_after,
            reclaimed,
        )
        return {
            "status": "ok",
            "size_before": size_before,
            "size_after": size_after,
            "reclaimed_bytes": reclaimed,
        }

    @governed_background_mutation
    async def vacuum_governed(self) -> Dict[str, Any]:
        return await self.vacuum()

    @staticmethod
    def _db_size_bytes() -> int:
        try:
            return os.path.getsize(DB_PATH)
        except OSError:
            return 0


db_vacuum_service = DBVacuumService()
