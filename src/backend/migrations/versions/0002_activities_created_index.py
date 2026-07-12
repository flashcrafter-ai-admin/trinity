"""activities created_at index (#1265)

Adds ``idx_activities_created`` on ``agent_activities(created_at DESC)`` for the
cross-agent timeline, which sorts by ``created_at DESC`` with no ``agent_name``
predicate — a sort the composite ``idx_activities_agent`` cannot serve, so it
degraded to a scan+sort as activity volume grew with fleet size.

Fresh PG builds already get this index because ``0001_baseline`` iterates
``db/schema.py:INDEXES`` (which now includes it). This revision exists so an
*existing* PG deployment — stamped at ``0001_baseline`` and never re-running
baseline — also picks the index up on ``alembic upgrade head``. Mirrors the
SQLite ``activities_created_index`` migration in ``db/migrations.py`` (#1265).

Revision ID: 0002_activities_created_index
Revises: 0001_baseline
Create Date: 2026-06-19
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_activities_created_index"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_activities_created "
        "ON agent_activities(created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_activities_created")
