"""Projected session state: runtime state, connection state, evidence time, active run.

Revision ID: 0002_session_state
Revises: 0001_baseline
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_session_state"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

RUNTIME_STATES = "('running', 'waiting_for_input', 'idle', 'failed', 'stopped', 'unknown')"
CONNECTION_STATES = "('connected', 'reconnecting', 'stale', 'disconnected')"


def upgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.add_column(
            sa.Column("runtime_state", sa.String(), server_default=sa.text("'unknown'"), nullable=False)
        )
        batch.add_column(
            sa.Column(
                "connection_state", sa.String(), server_default=sa.text("'disconnected'"), nullable=False
            )
        )
        batch.add_column(sa.Column("state_evidence_at", sa.String(27), nullable=True))
        batch.add_column(sa.Column("state_cursor", sa.Integer(), server_default=sa.text("0"), nullable=False))
        batch.add_column(sa.Column("active_run_id", sa.String(), nullable=True))
        batch.create_check_constraint("runtime_state", f"runtime_state IN {RUNTIME_STATES}")
        batch.create_check_constraint("connection_state", f"connection_state IN {CONNECTION_STATES}")


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
