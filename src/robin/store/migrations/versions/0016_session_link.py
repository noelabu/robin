"""Sessions continued in another runtime, linked to the session they came from.

Revision ID: 0016_session_link
Revises: 0015_skip_permissions
Create Date: 2026-10-18
"""

from alembic import op

revision = "0016_session_link"
down_revision = "0015_skip_permissions"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE session_link (
        link_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        source_session_id VARCHAR NOT NULL,
        target_session_id VARCHAR NOT NULL,
        source_runtime VARCHAR NOT NULL,
        target_runtime VARCHAR NOT NULL,
        source_path TEXT NOT NULL,
        written_path TEXT NOT NULL,
        written_size INTEGER NOT NULL,
        written_mtime_ns INTEGER NOT NULL,
        preview_hash VARCHAR NOT NULL,
        manifest TEXT NOT NULL,
        action_id VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_session_link PRIMARY KEY (link_id),
        CONSTRAINT uq_session_link_target_session_id UNIQUE (target_session_id),
        CONSTRAINT uq_session_link_action_id UNIQUE (action_id),
        CONSTRAINT ck_session_link_kind CHECK (kind IN ('continued')),
        CONSTRAINT fk_session_link_source_session_id_session FOREIGN KEY(source_session_id) REFERENCES session (session_id),
        CONSTRAINT fk_session_link_target_session_id_session FOREIGN KEY(target_session_id) REFERENCES session (session_id)
    )
    """,
    "CREATE INDEX ix_session_link_source_session_id ON session_link (source_session_id)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
