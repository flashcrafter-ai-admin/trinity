"""Bind idempotency claims to the exact request bytes.

Adds nullable ``request_digest`` to ``idempotency_keys`` on PostgreSQL. The
sealed task route uses it to reject reuse of an idempotency key with changed
request bytes. This mirrors the SQLite ``idempotency_request_digest`` migration
in ``db/migrations.py`` and the head schema in ``db/schema.py`` / ``db/tables.py``.

Fresh PostgreSQL builds already get the column through ``0001_baseline``;
``IF NOT EXISTS`` keeps this revision idempotent there.

Revision ID: 0019_idempotency_request_digest
Revises: 0018_schedule_executions_source_channel
Create Date: 2026-07-16
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_idempotency_request_digest"
down_revision = "0018_schedule_executions_source_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE idempotency_keys "
        "ADD COLUMN IF NOT EXISTS request_digest TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE idempotency_keys "
        "DROP COLUMN IF EXISTS request_digest"
    )
