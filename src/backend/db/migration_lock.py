"""Cross-process lock for the SQLite migration runner (#1160).

Kept dependency-light (stdlib only) and free of any import-time side effects so
it can be unit-tested in a spawned subprocess without dragging in the rest of
``database.py`` (whose module-level ``db = DatabaseManager()`` would run
``init_database()`` on import).
"""

import fcntl
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def migration_lock(db_path: str):
    """Hold an exclusive cross-process lock around the migration runner.

    Two uvicorn workers plus the scheduler container share the ``/data`` bind
    mount and each run the full migration suite on boot, which has caused boot
    failures (#456/#389). An OS ``flock`` on a sidecar file serialises them
    across processes — unlike a ``BEGIN IMMEDIATE`` DB lock, it is not released
    by the suite's many intra-run ``conn.commit()`` calls, and the kernel
    releases it automatically on process death (kill -9/OOM), so a crashed
    holder never leaves a stale lock. Assumes ``db_path``'s directory is a
    local-FS bind mount (flock is unreliable on NFS).

    Fails open: if the lock file can't be created or locked (e.g. EACCES on a
    pre-#874 root-owned path), the caller proceeds unlocked rather than hanging
    — that is the prior behavior, no worse than the status quo.
    """
    lock_path = f"{db_path}.migrate.lock"
    fd = None
    try:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            # LOCK_EX blocks with no timeout by design: the holder only ever runs
            # the (fast) migration suite and the kernel frees the lock on its
            # death, so the only way to wait forever is a genuinely hung
            # migration — which would block boot regardless of this lock.
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            logger.warning("migration lock unavailable (%s); proceeding without it", e)
            if fd is not None:
                os.close(fd)
                fd = None
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
