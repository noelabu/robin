"""Baseline schema: sessions, runs, connections, sources, events, quarantine, control actions, audit.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-15

Frozen DDL: later schema changes go in new revisions, never here.
"""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE blob (
        sha256 VARCHAR NOT NULL,
        size INTEGER NOT NULL,
        media_type VARCHAR NOT NULL,
        state VARCHAR DEFAULT 'present' NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_blob PRIMARY KEY (sha256),
        CONSTRAINT ck_blob_state CHECK (state IN ('present', 'missing'))
    )
    """,
    """
    CREATE TABLE meta (
        "key" VARCHAR NOT NULL,
        value TEXT NOT NULL,
        CONSTRAINT pk_meta PRIMARY KEY ("key")
    )
    """,
    """
    CREATE TABLE session (
        session_id VARCHAR NOT NULL,
        host_id VARCHAR NOT NULL,
        runtime VARCHAR NOT NULL,
        native_session_id VARCHAR,
        mode VARCHAR NOT NULL,
        execution_cwd TEXT,
        capability_snapshot TEXT,
        control_owner VARCHAR,
        lease_generation INTEGER DEFAULT 0 NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_session PRIMARY KEY (session_id),
        CONSTRAINT uq_session_host_id_runtime_native_session_id UNIQUE (host_id, runtime, native_session_id),
        CONSTRAINT ck_session_runtime_nonempty CHECK (runtime <> ''),
        CONSTRAINT ck_session_mode CHECK (mode IN ('managed', 'observed'))
    )
    """,
    """
    CREATE TABLE connection (
        connection_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        connection_generation INTEGER NOT NULL,
        supervisor_generation VARCHAR,
        state VARCHAR NOT NULL,
        coverage TEXT,
        cursor INTEGER,
        opened_at VARCHAR(27) NOT NULL,
        last_seen_at VARCHAR(27),
        closed_at VARCHAR(27),
        CONSTRAINT pk_connection PRIMARY KEY (connection_id),
        CONSTRAINT uq_connection_session_id_connection_generation UNIQUE (session_id, connection_generation),
        CONSTRAINT ck_connection_state CHECK (state IN ('connected', 'reconnecting', 'stale', 'disconnected')),
        CONSTRAINT fk_connection_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id)
    )
    """,
    """
    CREATE TABLE control_action (
        action_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        action_type VARCHAR NOT NULL,
        target VARCHAR,
        request_hash VARCHAR NOT NULL,
        expected_generation INTEGER,
        payload_inline TEXT,
        payload_blob VARCHAR,
        state VARCHAR NOT NULL,
        native_correlation VARCHAR,
        error_code VARCHAR,
        error_message TEXT,
        requested_at VARCHAR(27) NOT NULL,
        submitted_at VARCHAR(27),
        acknowledged_at VARCHAR(27),
        completed_at VARCHAR(27),
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_control_action PRIMARY KEY (action_id),
        CONSTRAINT ck_control_action_state CHECK (state IN ('requested', 'submitted', 'acknowledged', 'failed', 'cancelled', 'outcome_unknown')),
        CONSTRAINT fk_control_action_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_control_action_payload_blob_blob FOREIGN KEY(payload_blob) REFERENCES blob (sha256)
    )
    """,
    """
    CREATE INDEX ix_control_action_session_state ON control_action (session_id, state)
    """,
    """
    CREATE TABLE run (
        run_id VARCHAR NOT NULL,
        session_id VARCHAR NOT NULL,
        native_turn_id VARCHAR,
        started_at VARCHAR(27) NOT NULL,
        ended_at VARCHAR(27),
        outcome VARCHAR,
        CONSTRAINT pk_run PRIMARY KEY (run_id),
        CONSTRAINT uq_run_session_id_native_turn_id UNIQUE (session_id, native_turn_id),
        CONSTRAINT ck_run_outcome CHECK (outcome IS NULL OR outcome IN ('completed', 'interrupted', 'failed', 'stopped', 'unknown')),
        CONSTRAINT fk_run_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id)
    )
    """,
    """
    CREATE TABLE source_checkpoint (
        source_id VARCHAR NOT NULL,
        generation INTEGER NOT NULL,
        kind VARCHAR NOT NULL,
        generation_label VARCHAR,
        session_id VARCHAR,
        runtime VARCHAR NOT NULL,
        path TEXT,
        file_dev INTEGER,
        file_ino INTEGER,
        "offset" INTEGER DEFAULT 0 NOT NULL,
        anchor_hash VARCHAR,
        state VARCHAR NOT NULL,
        reason VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_source_checkpoint PRIMARY KEY (source_id, generation),
        CONSTRAINT ck_source_checkpoint_kind CHECK (kind IN ('supervisor', 'jsonl_file')),
        CONSTRAINT ck_source_checkpoint_state CHECK (state IN ('active', 'superseded')),
        CONSTRAINT ck_source_checkpoint_reason CHECK (reason IN ('initial', 'rotated', 'truncated', 'rewritten')),
        CONSTRAINT fk_source_checkpoint_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id)
    )
    """,
    """
    CREATE UNIQUE INDEX ix_source_checkpoint_active ON source_checkpoint (source_id) WHERE state = 'active'
    """,
    """
    CREATE TABLE audit_entry (
        audit_id VARCHAR NOT NULL,
        at VARCHAR(27) NOT NULL,
        actor VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        affected_ids TEXT NOT NULL,
        outcome VARCHAR NOT NULL,
        correlation_id VARCHAR,
        action_id VARCHAR,
        CONSTRAINT pk_audit_entry PRIMARY KEY (audit_id),
        CONSTRAINT ck_audit_entry_actor CHECK (actor IN ('user', 'system')),
        CONSTRAINT fk_audit_entry_action_id_control_action FOREIGN KEY(action_id) REFERENCES control_action (action_id)
    )
    """,
    """
    CREATE INDEX ix_audit_entry_at ON audit_entry (at)
    """,
    """
    CREATE TABLE event (
        global_cursor INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        event_id VARCHAR NOT NULL,
        schema_version INTEGER NOT NULL,
        session_id VARCHAR NOT NULL,
        runtime VARCHAR NOT NULL,
        native_session_id VARCHAR,
        run_id VARCHAR,
        source_id VARCHAR NOT NULL,
        source_generation INTEGER NOT NULL,
        source_event_id VARCHAR,
        source_seq INTEGER NOT NULL,
        source_offset_end INTEGER,
        dedup_key VARCHAR NOT NULL,
        native_event_id VARCHAR,
        duplicate_of INTEGER,
        source_time VARCHAR(27),
        ingested_at VARCHAR(27) NOT NULL,
        event_family VARCHAR NOT NULL,
        event_type VARCHAR NOT NULL,
        evidence_kind VARCHAR NOT NULL,
        payload_inline TEXT,
        payload_blob VARCHAR,
        payload_size INTEGER NOT NULL,
        content_hash VARCHAR NOT NULL,
        CONSTRAINT uq_event_source_id_dedup_key UNIQUE (source_id, dedup_key),
        CONSTRAINT fk_event_source_checkpoint FOREIGN KEY(source_id, source_generation) REFERENCES source_checkpoint (source_id, generation),
        CONSTRAINT ck_event_one_payload CHECK ((payload_inline IS NULL) <> (payload_blob IS NULL)),
        CONSTRAINT ck_event_evidence_kind CHECK (evidence_kind IN ('reported', 'observed', 'inferred', 'unknown')),
        CONSTRAINT uq_event_event_id UNIQUE (event_id),
        CONSTRAINT fk_event_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id),
        CONSTRAINT fk_event_run_id_run FOREIGN KEY(run_id) REFERENCES run (run_id),
        CONSTRAINT fk_event_duplicate_of_event FOREIGN KEY(duplicate_of) REFERENCES event (global_cursor),
        CONSTRAINT fk_event_payload_blob_blob FOREIGN KEY(payload_blob) REFERENCES blob (sha256)
    )
    """,
    """
    CREATE INDEX ix_event_native ON event (session_id, native_event_id) WHERE native_event_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_event_run ON event (run_id) WHERE run_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_event_session_cursor ON event (session_id, global_cursor)
    """,
    """
    CREATE INDEX ix_event_source_hash ON event (source_id, content_hash)
    """,
    """
    CREATE INDEX ix_event_source_order ON event (source_id, source_generation, source_seq)
    """,
    """
    CREATE TABLE quarantine (
        quarantine_id VARCHAR NOT NULL,
        source_id VARCHAR NOT NULL,
        generation INTEGER NOT NULL,
        offset_start INTEGER NOT NULL,
        offset_end INTEGER NOT NULL,
        reason VARCHAR NOT NULL,
        diagnostic TEXT NOT NULL,
        byte_length INTEGER NOT NULL,
        content_hash VARCHAR,
        payload_blob VARCHAR,
        created_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_quarantine PRIMARY KEY (quarantine_id),
        CONSTRAINT uq_quarantine_source_id_generation_offset_start UNIQUE (source_id, generation, offset_start),
        CONSTRAINT fk_quarantine_source_checkpoint FOREIGN KEY(source_id, generation) REFERENCES source_checkpoint (source_id, generation),
        CONSTRAINT ck_quarantine_reason CHECK (reason IN ('oversized', 'invalid_json', 'mapper_error', 'unknown_session', 'incomplete_at_rotation')),
        CONSTRAINT fk_quarantine_payload_blob_blob FOREIGN KEY(payload_blob) REFERENCES blob (sha256)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
