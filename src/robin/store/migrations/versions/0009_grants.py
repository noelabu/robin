"""Agent credentials, retrieval grants and reads (FR-022, FR-023, TR-066, TR-068).

Revision ID: 0009_grants
Revises: 0008_context
Create Date: 2026-10-15
"""

from alembic import op

revision = "0009_grants"
down_revision = "0008_context"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE agent_credential (
        session_id VARCHAR NOT NULL,
        token_hash VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_agent_credential PRIMARY KEY (session_id),
        CONSTRAINT fk_agent_credential_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT uq_agent_credential_token_hash UNIQUE (token_hash)
    )
    """,
    """
    CREATE TABLE retrieval_grant (
        grant_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        task_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        artifact_id VARCHAR,
        version INTEGER,
        excerpt_session_id VARCHAR,
        after_cursor INTEGER,
        through_cursor INTEGER,
        approved_by VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        revoked_at VARCHAR(27),
        CONSTRAINT pk_retrieval_grant PRIMARY KEY (grant_id),
        CONSTRAINT ck_retrieval_grant_kind CHECK (kind IN ('artifact', 'excerpt')),
        CONSTRAINT fk_retrieval_grant_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_retrieval_grant_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_retrieval_grant_artifact_id_artifact FOREIGN KEY(artifact_id) REFERENCES artifact (artifact_id),
        CONSTRAINT fk_retrieval_grant_excerpt_session_id_session FOREIGN KEY(excerpt_session_id) REFERENCES session (session_id),
        CONSTRAINT fk_retrieval_grant_approved_by_audit_entry FOREIGN KEY(approved_by) REFERENCES audit_entry (audit_id)
    )
    """,
    """
    CREATE INDEX ix_retrieval_grant_session ON retrieval_grant (session_id, revoked_at)
    """,
    """
    CREATE TABLE retrieval_read (
        read_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        tool VARCHAR NOT NULL,
        request TEXT NOT NULL,
        grant_ids TEXT NOT NULL,
        content_blob VARCHAR NOT NULL,
        size INTEGER NOT NULL,
        at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_retrieval_read PRIMARY KEY (read_id),
        CONSTRAINT fk_retrieval_read_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_retrieval_read_content_blob_blob FOREIGN KEY(content_blob) REFERENCES blob (sha256)
    )
    """,
    """
    CREATE INDEX ix_retrieval_read_session ON retrieval_read (session_id, at)
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
