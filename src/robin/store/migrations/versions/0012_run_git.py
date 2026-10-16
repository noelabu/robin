"""Git context per run: repository, branch, base commit, working-tree state, changed files (FR-050, TR-075).

Revision ID: 0012_run_git
Revises: 0011_retention
Create Date: 2026-10-16
"""

from alembic import op

revision = "0012_run_git"
down_revision = "0011_retention"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE run_git (
        run_id VARCHAR NOT NULL,
        repo_root VARCHAR,
        branch VARCHAR,
        base_commit VARCHAR,
        start_dirty INTEGER,
        start_snapshot TEXT,
        start_late INTEGER DEFAULT 0 NOT NULL,
        start_captured_at VARCHAR(27) NOT NULL,
        end_commit VARCHAR,
        end_dirty INTEGER,
        changed_files TEXT,
        shared_folder INTEGER DEFAULT 0 NOT NULL,
        truncated INTEGER DEFAULT 0 NOT NULL,
        end_captured_at VARCHAR(27),
        CONSTRAINT pk_run_git PRIMARY KEY (run_id),
        CONSTRAINT fk_run_git_run_id_run FOREIGN KEY(run_id) REFERENCES run (run_id)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
