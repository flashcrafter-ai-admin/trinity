"""Shared query helpers (#1265).

Small, backend-agnostic SQLAlchemy Core building blocks reused by more than one
``XOperations`` class so the window/aggregation SQL lives in one place instead
of being copy-pasted per domain.
"""
from __future__ import annotations

from typing import Iterable, List

from sqlalchemy import select, func
from sqlalchemy import Column

from .engine import get_engine


def latest_per_group(
    columns,
    partition_col: "Column",
    order_col: "Column",
    filter_col: "Column",
    values: Iterable,
) -> List:
    """Return the newest row per group in a single query.

    Runs ``ROW_NUMBER() OVER (PARTITION BY partition_col ORDER BY order_col
    DESC)`` over the rows where ``filter_col IN values`` and keeps ``rn == 1``
    per group. Returns name-accessible mapping rows (the ``rn`` helper column is
    dropped), so callers map by column name — never by position.

    Keep ``columns`` narrow: project only what the caller needs, not the whole
    table (avoids streaming large TEXT blobs the caller never reads). Empty
    ``values`` short-circuits to ``[]``.
    """
    values = list(values)
    if not values:
        return []

    rn = func.row_number().over(
        partition_by=partition_col,
        order_by=order_col.desc(),
    ).label("rn")
    subq = select(*columns, rn).where(filter_col.in_(values)).subquery()
    # Drop the trailing rn helper column; keep the projected columns by name.
    projected = list(subq.c)[:-1]
    stmt = select(*projected).where(subq.c.rn == 1)

    with get_engine().connect() as conn:
        return conn.execute(stmt).mappings().all()
