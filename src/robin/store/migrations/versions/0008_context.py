"""Context packets and deliveries (FR-021-024, TR-061-065, TR-069).

Revision ID: 0008_context
Revises: 0007_independence
Create Date: 2026-10-15
"""

from alembic import op

revision = "0008_context"
down_revision = "0007_independence"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE context_packet (
        packet_id VARCHAR NOT NULL,
        task_id VARCHAR NOT NULL,
        target_session_id VARCHAR NOT NULL,
        target_generation INTEGER NOT NULL,
        state VARCHAR NOT NULL,
        payload_blob VARCHAR NOT NULL,
        payload_hash VARCHAR NOT NULL,
        size INTEGER NOT NULL,
        manifest TEXT NOT NULL,
        artifact_id VARCHAR,
        invalid_reason TEXT,
        created_at VARCHAR(27) NOT NULL,
        confirmed_at VARCHAR(27),
        CONSTRAINT pk_context_packet PRIMARY KEY (packet_id),
        CONSTRAINT ck_context_packet_state CHECK (state IN ('previewed', 'confirmed', 'invalidated', 'delivered')),
        CONSTRAINT fk_context_packet_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_context_packet_target_session_id_session FOREIGN KEY(target_session_id) REFERENCES session (session_id),
        CONSTRAINT fk_context_packet_payload_blob_blob FOREIGN KEY(payload_blob) REFERENCES blob (sha256),
        CONSTRAINT fk_context_packet_artifact_id_artifact FOREIGN KEY(artifact_id) REFERENCES artifact (artifact_id)
    )
    """,
    """
    CREATE INDEX ix_context_packet_task ON context_packet (task_id, created_at)
    """,
    """
    CREATE TABLE context_delivery (
        delivery_id VARCHAR NOT NULL,
        packet_id VARCHAR NOT NULL,
        action_id VARCHAR NOT NULL,
        target_session_id VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_context_delivery PRIMARY KEY (delivery_id),
        CONSTRAINT fk_context_delivery_packet_id_context_packet FOREIGN KEY(packet_id) REFERENCES context_packet (packet_id),
        CONSTRAINT uq_context_delivery_action_id UNIQUE (action_id),
        CONSTRAINT fk_context_delivery_action_id_control_action FOREIGN KEY(action_id) REFERENCES control_action (action_id),
        CONSTRAINT fk_context_delivery_target_session_id_session FOREIGN KEY(target_session_id) REFERENCES session (session_id)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
