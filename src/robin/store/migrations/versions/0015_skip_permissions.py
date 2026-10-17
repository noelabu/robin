"""Sessions launched with permission prompts skipped, kept across relaunches (FR-012).

Revision ID: 0015_skip_permissions
Revises: 0014_session_activity
Create Date: 2026-10-17
"""

from alembic import op

revision = "0015_skip_permissions"
down_revision = "0014_session_activity"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE session ADD COLUMN skip_permissions INTEGER DEFAULT 0 NOT NULL",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
