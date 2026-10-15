"""Attention items: questions, approvals, failures and findings awaiting review (FR-017, FR-035, TR-045).

Revision ID: 0004_attention
Revises: 0003_projects_tasks
Create Date: 2026-10-15
"""

from alembic import op

revision = "0004_attention"
down_revision = "0003_projects_tasks"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE attention_item (
        attention_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        task_id VARCHAR,
        run_id VARCHAR,
        native_request_id VARCHAR,
        state VARCHAR NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL,
        allowed_responses TEXT NOT NULL,
        originating_cursor INTEGER NOT NULL,
        resolution_action_id VARCHAR,
        resolution VARCHAR,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_attention_item PRIMARY KEY (attention_id),
        CONSTRAINT ck_attention_item_kind CHECK (kind IN ('question', 'approval', 'failure', 'finding_review')),
        CONSTRAINT ck_attention_item_state CHECK (state IN ('open', 'acknowledged', 'resolved', 'dismissed', 'expired', 'handed_off')),
        CONSTRAINT uq_attention_item_session_id_native_request_id UNIQUE (session_id, native_request_id),
        CONSTRAINT fk_attention_item_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_attention_item_task_id_task FOREIGN KEY(task_id) REFERENCES task (task_id),
        CONSTRAINT fk_attention_item_run_id_run FOREIGN KEY(run_id) REFERENCES run (run_id),
        CONSTRAINT fk_attention_item_originating_cursor_event FOREIGN KEY(originating_cursor) REFERENCES event (global_cursor)
    )
    """,
    """
    CREATE INDEX ix_attention_item_state ON attention_item (state, updated_at)
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
