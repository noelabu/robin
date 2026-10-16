"""Worktrees Robin creates for sessions (FR-051, TR-070, TR-072).

Revision ID: 0013_worktrees
Revises: 0012_run_git
Create Date: 2026-10-16
"""

from alembic import op

revision = "0013_worktrees"
down_revision = "0012_run_git"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE worktree (
        worktree_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        task_id VARCHAR,
        repo_root VARCHAR NOT NULL,
        path VARCHAR NOT NULL,
        branch VARCHAR NOT NULL,
        from_branch VARCHAR,
        base_commit VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        removed_at VARCHAR(27),
        CONSTRAINT pk_worktree PRIMARY KEY (worktree_id),
        CONSTRAINT fk_worktree_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_worktree_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
