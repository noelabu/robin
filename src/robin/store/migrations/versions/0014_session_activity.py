"""What each session is doing, derived from its events for the Map: title, last tool, todos, files.

Revision ID: 0014_session_activity
Revises: 0013_worktrees
Create Date: 2026-10-17
"""

from alembic import op

revision = "0014_session_activity"
down_revision = "0013_worktrees"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE session_activity (
        session_id VARCHAR NOT NULL,
        lane VARCHAR NOT NULL,
        title TEXT,
        label VARCHAR,
        tool VARCHAR,
        target TEXT,
        action_at VARCHAR(27),
        turn_started_at VARCHAR(27),
        last_activity_at VARCHAR(27),
        todos TEXT NOT NULL,
        todo_done INTEGER NOT NULL,
        todo_total INTEGER NOT NULL,
        files TEXT NOT NULL,
        files_count INTEGER NOT NULL,
        scratch TEXT NOT NULL,
        CONSTRAINT pk_session_activity PRIMARY KEY (session_id, lane),
        CONSTRAINT fk_session_activity_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id)
    )
    """,
    "CREATE INDEX ix_session_activity_last_activity_at ON session_activity (last_activity_at)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
