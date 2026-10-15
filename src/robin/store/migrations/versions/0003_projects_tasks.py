"""Projects, tasks, task-session associations and attachments; task and project links (FR-001–003).

Revision ID: 0003_projects_tasks
Revises: 0002_session_state
Create Date: 2026-10-15

New columns on existing tables are added with ALTER TABLE ... REFERENCES (nullable, no default), which
SQLite does without rebuilding the table, so the event table is never copied.
"""

from alembic import op

revision = "0003_projects_tasks"
down_revision = "0002_session_state"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE project (
        project_id VARCHAR NOT NULL,
        path TEXT NOT NULL,
        name VARCHAR NOT NULL,
        git_remote TEXT,
        git_branch VARCHAR,
        git_checked_at VARCHAR(27),
        archived INTEGER DEFAULT 0 NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_project PRIMARY KEY (project_id),
        CONSTRAINT uq_project_path UNIQUE (path)
    )
    """,
    """
    CREATE TABLE task (
        task_id VARCHAR NOT NULL,
        project_id VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        objective TEXT NOT NULL,
        status VARCHAR DEFAULT 'open' NOT NULL,
        completion_evidence TEXT,
        archived INTEGER DEFAULT 0 NOT NULL,
        revision INTEGER DEFAULT 1 NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_task PRIMARY KEY (task_id),
        CONSTRAINT ck_task_status CHECK (status IN ('open', 'completed', 'abandoned')),
        CONSTRAINT ck_task_completion_evidence CHECK (status <> 'completed' OR completion_evidence IS NOT NULL),
        CONSTRAINT fk_task_project_id_project FOREIGN KEY(project_id) REFERENCES project (project_id)
    )
    """,
    """
    CREATE INDEX ix_task_project ON task (project_id, archived)
    """,
    """
    CREATE TABLE task_attachment (
        attachment_id VARCHAR NOT NULL,
        task_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        source_path TEXT,
        text_value TEXT,
        snapshot_blob VARCHAR,
        content_hash VARCHAR,
        size INTEGER,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_task_attachment PRIMARY KEY (attachment_id),
        CONSTRAINT ck_task_attachment_kind CHECK (kind IN ('file', 'instruction', 'expected_output')),
        CONSTRAINT fk_task_attachment_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_task_attachment_snapshot_blob_blob FOREIGN KEY(snapshot_blob) REFERENCES blob (sha256)
    )
    """,
    """
    CREATE TABLE task_session (
        task_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        role VARCHAR,
        retained_context_disclosed INTEGER DEFAULT 0 NOT NULL,
        attached_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_task_session PRIMARY KEY (task_id, session_id),
        CONSTRAINT fk_task_session_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_task_session_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id)
    )
    """,
    """
    ALTER TABLE session ADD COLUMN project_id VARCHAR CONSTRAINT fk_session_project_id_project REFERENCES project (project_id)
    """,
    """
    ALTER TABLE session ADD COLUMN current_task_id VARCHAR CONSTRAINT fk_session_current_task_id_task REFERENCES task (task_id)
    """,
    """
    ALTER TABLE session ADD COLUMN pending_task_id VARCHAR CONSTRAINT fk_session_pending_task_id_task REFERENCES task (task_id)
    """,
    """
    ALTER TABLE run ADD COLUMN task_id VARCHAR CONSTRAINT fk_run_task_id_task REFERENCES task (task_id)
    """,
    """
    ALTER TABLE event ADD COLUMN task_id VARCHAR CONSTRAINT fk_event_task_id_task REFERENCES task (task_id)
    """,
    """
    ALTER TABLE session ADD COLUMN archived INTEGER DEFAULT 0 NOT NULL
    """,
    """
    CREATE INDEX ix_event_task ON event (task_id, global_cursor) WHERE task_id IS NOT NULL
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
