"""SQLAlchemy models for Robin's database (TR-020, TR-023). The Alembic migrations must match these."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from robin.store.db import UTCDateTime

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


SESSION_MODES = ("managed", "observed")
RUNTIME_STATES = ("running", "waiting_for_input", "idle", "failed", "stopped", "unknown")
RUN_OUTCOMES = ("completed", "interrupted", "failed", "stopped", "unknown")
CONNECTION_STATES = ("connected", "reconnecting", "stale", "disconnected")
SOURCE_KINDS = ("supervisor", "jsonl_file", "hook")
SOURCE_STATES = ("active", "superseded")
GENERATION_REASONS = ("initial", "rotated", "truncated", "rewritten")
BLOB_STATES = ("present", "missing")
EVIDENCE_KINDS = ("reported", "observed", "inferred", "unknown")
QUARANTINE_REASONS = (
    "oversized",
    "invalid_json",
    "mapper_error",
    "unknown_session",
    "incomplete_at_rotation",
)
ACTION_STATES = ("requested", "submitted", "acknowledged", "failed", "cancelled", "outcome_unknown")
ACTORS = ("user", "system")
TASK_STATUSES = ("open", "completed", "abandoned")
ATTENTION_KINDS = ("question", "approval", "failure", "finding_review")
ATTENTION_STATES = ("open", "acknowledged", "resolved", "dismissed", "expired", "handed_off")
ATTACHMENT_KINDS = ("file", "instruction", "expected_output")


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (
        # Native session identity (TR-023). NULL native IDs don't collide in SQLite.
        UniqueConstraint("host_id", "runtime", "native_session_id"),
        CheckConstraint("runtime <> ''", name="runtime_nonempty"),
        CheckConstraint(_in("mode", SESSION_MODES), name="mode"),
        CheckConstraint(_in("runtime_state", RUNTIME_STATES), name="runtime_state"),
        CheckConstraint(_in("connection_state", CONNECTION_STATES), name="connection_state"),
    )
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    host_id: Mapped[str] = mapped_column(String)
    runtime: Mapped[str] = mapped_column(String)
    native_session_id: Mapped[str | None] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    execution_cwd: Mapped[str | None] = mapped_column(Text)
    capability_snapshot: Mapped[str | None] = mapped_column(Text)
    control_owner: Mapped[str | None] = mapped_column(String)
    lease_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Projected state (TR-040): runtime and connection are separate, each with its evidence time.
    runtime_state: Mapped[str] = mapped_column(String, server_default=text("'unknown'"))
    connection_state: Mapped[str] = mapped_column(String, server_default=text("'disconnected'"))
    state_evidence_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    state_cursor: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    active_run_id: Mapped[str | None] = mapped_column(String)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.project_id"))
    # The task the next run is bound to; a confirmed re-attach takes effect at the next turn (TR-020).
    current_task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))
    pending_task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))
    archived: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class Run(Base):
    __tablename__ = "run"
    __table_args__ = (
        UniqueConstraint("session_id", "native_turn_id"),
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', RUN_OUTCOMES)}", name="outcome"),
    )
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    native_turn_id: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    outcome: Mapped[str | None] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))  # fixed when the run starts


class Connection(Base):
    __tablename__ = "connection"
    __table_args__ = (
        UniqueConstraint("session_id", "connection_generation"),
        CheckConstraint(_in("state", CONNECTION_STATES), name="state"),
    )
    connection_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    connection_generation: Mapped[int] = mapped_column(Integer)
    supervisor_generation: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    coverage: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[int | None] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SourceCheckpoint(Base):
    """Ingestion position for one generation of one source (TR-033)."""

    __tablename__ = "source_checkpoint"
    __table_args__ = (
        CheckConstraint(_in("kind", SOURCE_KINDS), name="kind"),
        CheckConstraint(_in("state", SOURCE_STATES), name="state"),
        CheckConstraint(_in("reason", GENERATION_REASONS), name="reason"),
        # Exactly one active generation per source.
        Index("ix_source_checkpoint_active", "source_id", unique=True, sqlite_where=text("state = 'active'")),
    )
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(String)
    generation_label: Mapped[str | None] = mapped_column(String)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.session_id"))
    runtime: Mapped[str] = mapped_column(String)
    path: Mapped[str | None] = mapped_column(Text)
    file_dev: Mapped[int | None] = mapped_column(Integer)
    file_ino: Mapped[int | None] = mapped_column(Integer)
    offset: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    anchor_hash: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Blob(Base):
    """A content-addressed file in the artifact store."""

    __tablename__ = "blob"
    __table_args__ = (CheckConstraint(_in("state", BLOB_STATES), name="state"),)
    sha256: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, server_default=text("'present'"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Event(Base):
    """One normalized event (TR-030). global_cursor is commit order and is never reused."""

    __tablename__ = "event"
    __table_args__ = (
        # Source-qualified native event identity (TR-023, TR-032).
        UniqueConstraint("source_id", "dedup_key"),
        ForeignKeyConstraint(
            ["source_id", "source_generation"],
            ["source_checkpoint.source_id", "source_checkpoint.generation"],
            name="fk_event_source_checkpoint",
        ),
        CheckConstraint("(payload_inline IS NULL) <> (payload_blob IS NULL)", name="one_payload"),
        CheckConstraint(_in("evidence_kind", EVIDENCE_KINDS), name="evidence_kind"),
        Index("ix_event_session_cursor", "session_id", "global_cursor"),
        Index("ix_event_source_order", "source_id", "source_generation", "source_seq"),
        Index("ix_event_source_hash", "source_id", "content_hash"),
        Index(
            "ix_event_native",
            "session_id",
            "native_event_id",
            sqlite_where=text("native_event_id IS NOT NULL"),
        ),
        Index("ix_event_run", "run_id", sqlite_where=text("run_id IS NOT NULL")),
        Index("ix_event_task", "task_id", "global_cursor", sqlite_where=text("task_id IS NOT NULL")),
        {"sqlite_autoincrement": True},
    )
    global_cursor: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    runtime: Mapped[str] = mapped_column(String)
    native_session_id: Mapped[str | None] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("run.run_id"))
    source_id: Mapped[str] = mapped_column(String)
    source_generation: Mapped[int] = mapped_column(Integer)
    source_event_id: Mapped[str | None] = mapped_column(String)
    source_seq: Mapped[int] = mapped_column(Integer)
    source_offset_end: Mapped[int | None] = mapped_column(Integer)
    dedup_key: Mapped[str] = mapped_column(String)
    native_event_id: Mapped[str | None] = mapped_column(String)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("event.global_cursor"))
    source_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    event_family: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    evidence_kind: Mapped[str] = mapped_column(String)
    # TEXT, not BLOB: SQLite's json functions treat BLOB arguments as JSONB.
    payload_inline: Mapped[str | None] = mapped_column(Text)
    payload_blob: Mapped[str | None] = mapped_column(ForeignKey("blob.sha256"))
    payload_size: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))


class Quarantine(Base):
    """A record that couldn't be ingested. Diagnostics hold no content (TR-033, TR-083)."""

    __tablename__ = "quarantine"
    __table_args__ = (
        UniqueConstraint("source_id", "generation", "offset_start"),
        ForeignKeyConstraint(
            ["source_id", "generation"],
            ["source_checkpoint.source_id", "source_checkpoint.generation"],
            name="fk_quarantine_source_checkpoint",
        ),
        CheckConstraint(_in("reason", QUARANTINE_REASONS), name="reason"),
    )
    quarantine_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String)
    generation: Mapped[int] = mapped_column(Integer)
    offset_start: Mapped[int] = mapped_column(Integer)
    offset_end: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String)
    diagnostic: Mapped[str] = mapped_column(Text)
    byte_length: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String)
    payload_blob: Mapped[str | None] = mapped_column(ForeignKey("blob.sha256"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ControlAction(Base):
    """A user control request; persisted before dispatch (TR-041). Behaviour arrives in WP-1.4."""

    __tablename__ = "control_action"
    __table_args__ = (
        CheckConstraint(_in("state", ACTION_STATES), name="state"),
        Index("ix_control_action_session_state", "session_id", "state"),
    )
    action_id: Mapped[str] = mapped_column(String, primary_key=True)  # client action ID (TR-023)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    action_type: Mapped[str] = mapped_column(String)
    target: Mapped[str | None] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    expected_generation: Mapped[int | None] = mapped_column(Integer)
    payload_inline: Mapped[str | None] = mapped_column(Text)
    payload_blob: Mapped[str | None] = mapped_column(ForeignKey("blob.sha256"))
    state: Mapped[str] = mapped_column(String)
    native_correlation: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditEntry(Base):
    __tablename__ = "audit_entry"
    __table_args__ = (
        CheckConstraint(_in("actor", ACTORS), name="actor"),
        Index("ix_audit_entry_at", "at"),
    )
    audit_id: Mapped[str] = mapped_column(String, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    affected_ids: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[str | None] = mapped_column(String)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("control_action.action_id"))


class Project(Base):
    """A registered repository or directory (FR-001)."""

    __tablename__ = "project"
    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True)  # canonical
    name: Mapped[str] = mapped_column(String)
    git_remote: Mapped[str | None] = mapped_column(Text)
    git_branch: Mapped[str | None] = mapped_column(String)
    git_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    archived: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Task(Base):
    """The shared coordination boundary across agents (FR-002, FR-014)."""

    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint(_in("status", TASK_STATUSES), name="status"),
        # Completed only with recorded evidence of an explicit user decision.
        CheckConstraint(
            "status <> 'completed' OR completion_evidence IS NOT NULL", name="completion_evidence"
        ),
        Index("ix_task_project", "project_id", "archived"),
    )
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"))
    title: Mapped[str] = mapped_column(String)
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, server_default=text("'open'"))
    completion_evidence: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TaskSession(Base):
    """A session's association with a task; a session can appear in several task histories (TR-020)."""

    __tablename__ = "task_session"
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"), primary_key=True)
    role: Mapped[str | None] = mapped_column(String)
    retained_context_disclosed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attached_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TaskAttachment(Base):
    """Task input: an approved snapshot of a file, an instruction, or an expected output (FR-002)."""

    __tablename__ = "task_attachment"
    __table_args__ = (CheckConstraint(_in("kind", ATTACHMENT_KINDS), name="kind"),)
    attachment_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"))
    kind: Mapped[str] = mapped_column(String)
    source_path: Mapped[str | None] = mapped_column(Text)  # the original, never modified
    text_value: Mapped[str | None] = mapped_column(Text)
    snapshot_blob: Mapped[str | None] = mapped_column(ForeignKey("blob.sha256"))
    content_hash: Mapped[str | None] = mapped_column(String)
    size: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AttentionItem(Base):
    """Something that needs the user: a question, an approval, a failure, a finding to review (FR-035).

    Approvals and questions carry the runtime's own request ID; Robin-originated items have none (TR-045).
    Only an explicit response is routed to the runtime; acknowledging or dismissing never approves.
    """

    __tablename__ = "attention_item"
    __table_args__ = (
        CheckConstraint(_in("kind", ATTENTION_KINDS), name="kind"),
        CheckConstraint(_in("state", ATTENTION_STATES), name="state"),
        UniqueConstraint("session_id", "native_request_id"),
        Index("ix_attention_item_state", "state", "updated_at"),
    )
    attention_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("run.run_id"))
    native_request_id: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)  # JSON
    allowed_responses: Mapped[str] = mapped_column(Text)  # JSON list
    originating_cursor: Mapped[int] = mapped_column(ForeignKey("event.global_cursor"))
    # Taken before the action is recorded (the claim comes first), so it isn't a foreign key.
    resolution_action_id: Mapped[str | None] = mapped_column(String)
    resolution: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
