"""Retention: events whose payload was pruned under the storage cap (TR-081).

Revision ID: 0011_retention
Revises: 0010_search
Create Date: 2026-10-15
"""

from alembic import op

revision = "0011_retention"
down_revision = "0010_search"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE pruned_event (
        global_cursor INTEGER NOT NULL,
        original_size INTEGER NOT NULL,
        pruned_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_pruned_event PRIMARY KEY (global_cursor),
        CONSTRAINT fk_pruned_event_global_cursor_event FOREIGN KEY(global_cursor) REFERENCES event (global_cursor)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
