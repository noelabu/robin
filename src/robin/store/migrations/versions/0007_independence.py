"""Independent reviewers: record whether a session's independence is enforced (FR-032, TR-064).

Revision ID: 0007_independence
Revises: 0006_artifacts
Create Date: 2026-10-15
"""

from alembic import op

revision = "0007_independence"
down_revision = "0006_artifacts"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    ALTER TABLE session ADD COLUMN independence VARCHAR
        CONSTRAINT ck_session_independence CHECK (independence IS NULL OR independence IN ('enforced', 'not_isolated'))
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
