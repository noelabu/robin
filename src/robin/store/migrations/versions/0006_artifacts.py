"""Findings and artifacts: immutable versions, submissions, lineage (FR-020, TR-060, TR-064, TR-067).

Revision ID: 0006_artifacts
Revises: 0005_hook_source
Create Date: 2026-10-15
"""

from alembic import op

revision = "0006_artifacts"
down_revision = "0005_hook_source"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE artifact (
        artifact_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        task_id VARCHAR,
        author_kind VARCHAR NOT NULL,
        author_session_id VARCHAR,
        author_runtime VARCHAR,
        author_native_session_id VARCHAR,
        author_run_id VARCHAR,
        latest_version INTEGER NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_artifact PRIMARY KEY (artifact_id),
        CONSTRAINT ck_artifact_kind CHECK (kind IN ('finding', 'synthesis', 'packet')),
        CONSTRAINT ck_artifact_author_kind CHECK (author_kind IN ('user', 'agent')),
        CONSTRAINT fk_artifact_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_artifact_author_session_id_session FOREIGN KEY(author_session_id) REFERENCES session (session_id),
        CONSTRAINT fk_artifact_author_run_id_run FOREIGN KEY(author_run_id) REFERENCES run (run_id)
    )
    """,
    """
    CREATE INDEX ix_artifact_task ON artifact (task_id, kind, created_at)
    """,
    """
    CREATE TABLE artifact_version (
        artifact_id VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        content_blob VARCHAR NOT NULL,
        size INTEGER NOT NULL,
        submission_id VARCHAR,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_artifact_version PRIMARY KEY (artifact_id, version),
        CONSTRAINT fk_artifact_version_artifact_id_artifact FOREIGN KEY(artifact_id) REFERENCES artifact (artifact_id),
        CONSTRAINT fk_artifact_version_content_blob_blob FOREIGN KEY(content_blob) REFERENCES blob (sha256)
    )
    """,
    """
    CREATE TABLE finding_submission (
        submission_row_id VARCHAR NOT NULL,
        author_key VARCHAR NOT NULL,
        submission_id VARCHAR NOT NULL,
        session_id VARCHAR,
        task_id VARCHAR,
        path VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        errors TEXT NOT NULL,
        artifact_id VARCHAR,
        version INTEGER,
        event_cursor INTEGER,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_finding_submission PRIMARY KEY (submission_row_id),
        CONSTRAINT ck_finding_submission_path CHECK (path IN ('tool', 'message', 'user')),
        CONSTRAINT ck_finding_submission_state CHECK (state IN ('accepted', 'rejected')),
        CONSTRAINT uq_finding_submission_author_key_submission_id UNIQUE (author_key, submission_id),
        CONSTRAINT fk_finding_submission_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_finding_submission_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_finding_submission_artifact_id_artifact FOREIGN KEY(artifact_id) REFERENCES artifact (artifact_id),
        CONSTRAINT fk_finding_submission_event_cursor_event FOREIGN KEY(event_cursor) REFERENCES event (global_cursor)
    )
    """,
    """
    CREATE TABLE lineage_edge (
        child_artifact_id VARCHAR NOT NULL,
        child_version INTEGER NOT NULL,
        parent_artifact_id VARCHAR NOT NULL,
        parent_version INTEGER NOT NULL,
        relation VARCHAR NOT NULL,
        CONSTRAINT pk_lineage_edge PRIMARY KEY (child_artifact_id, child_version, parent_artifact_id, parent_version),
        CONSTRAINT ck_lineage_edge_relation CHECK (relation IN ('parent', 'input')),
        CONSTRAINT fk_lineage_edge_child FOREIGN KEY(child_artifact_id, child_version) REFERENCES artifact_version (artifact_id, version),
        CONSTRAINT fk_lineage_edge_parent_artifact_id_artifact FOREIGN KEY(parent_artifact_id) REFERENCES artifact (artifact_id)
    )
    """,
    """
    CREATE INDEX ix_lineage_edge_parent ON lineage_edge (parent_artifact_id)
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
