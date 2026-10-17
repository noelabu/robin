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
ARTIFACT_KINDS = ("finding", "synthesis", "packet")
AUTHOR_KINDS = ("user", "agent")
PUBLICATION_PATHS = ("tool", "message", "user")
SUBMISSION_STATES = ("accepted", "rejected")
LINEAGE_RELATIONS = ("parent", "input")
PACKET_STATES = ("previewed", "confirmed", "invalidated", "delivered")
GRANT_KINDS = ("artifact", "excerpt")
UNMODELED = ("search_index",)  # FTS5 virtual tables, defined only in migrations


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
        CheckConstraint(
            "independence IS NULL OR independence IN ('enforced', 'not_isolated')", name="independence"
        ),
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
    # An independent reviewer (TR-064): "enforced" when the sandbox excludes Robin's store, "not_isolated"
    # when the runtime can't; NULL for ordinary sessions.
    independence: Mapped[str | None] = mapped_column(String)
    # Launched with permission prompts skipped (an explicit opt-in); applied again on every relaunch.
    skip_permissions: Mapped[int] = mapped_column(Integer, server_default=text("0"))


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


class Artifact(Base):
    """Something Robin keeps for a task: a finding, a synthesis, an approved packet (FR-020, TR-060).

    Content lives in immutable versions in the artifact store, never in a project or execution folder.
    Authorship is fixed when the artifact is created, from the publishing session's launch identity.
    """

    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint(_in("kind", ARTIFACT_KINDS), name="kind"),
        CheckConstraint(_in("author_kind", AUTHOR_KINDS), name="author_kind"),
        Index("ix_artifact_task", "task_id", "kind", "created_at"),
    )
    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))
    author_kind: Mapped[str] = mapped_column(String)
    author_session_id: Mapped[str | None] = mapped_column(ForeignKey("session.session_id"))
    author_runtime: Mapped[str | None] = mapped_column(String)
    author_native_session_id: Mapped[str | None] = mapped_column(String)
    author_run_id: Mapped[str | None] = mapped_column(ForeignKey("run.run_id"))
    latest_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ArtifactVersion(Base):
    """One immutable version. The content is canonical JSON in a blob; its hash is the version's identity."""

    __tablename__ = "artifact_version"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.artifact_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    schema_version: Mapped[int] = mapped_column(Integer)
    content_blob: Mapped[str] = mapped_column(ForeignKey("blob.sha256"))
    size: Mapped[int] = mapped_column(Integer)
    submission_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FindingSubmission(Base):
    """Every finding submission, accepted or rejected (TR-067). A repeated submission ID from the same
    author returns the stored result instead of publishing again."""

    __tablename__ = "finding_submission"
    __table_args__ = (
        CheckConstraint(_in("path", PUBLICATION_PATHS), name="path"),
        CheckConstraint(_in("state", SUBMISSION_STATES), name="state"),
        UniqueConstraint("author_key", "submission_id"),
    )
    submission_row_id: Mapped[str] = mapped_column(String, primary_key=True)
    author_key: Mapped[str] = mapped_column(String)  # "session:<id>" or "user"
    submission_id: Mapped[str] = mapped_column(String)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.session_id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))
    path: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    errors: Mapped[str] = mapped_column(Text)  # JSON list
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifact.artifact_id"))
    version: Mapped[int | None] = mapped_column(Integer)
    event_cursor: Mapped[int | None] = mapped_column(ForeignKey("event.global_cursor"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LineageEdge(Base):
    """Where an artifact version came from: its parent findings or the inputs of a synthesis (TR-064)."""

    __tablename__ = "lineage_edge"
    __table_args__ = (
        CheckConstraint(_in("relation", LINEAGE_RELATIONS), name="relation"),
        ForeignKeyConstraint(
            ["child_artifact_id", "child_version"],
            ["artifact_version.artifact_id", "artifact_version.version"],
            name="fk_lineage_edge_child",
        ),
        Index("ix_lineage_edge_parent", "parent_artifact_id"),
    )
    child_artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    child_version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    parent_artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.artifact_id"), primary_key=True)
    parent_version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    relation: Mapped[str] = mapped_column(String)


class ContextPacket(Base):
    """A context transfer (FR-021–024, TR-061–065, TR-069): previewed, then confirmed, then delivered.

    The preview's exact payload, its hash and the source versions it was built from are stored; confirming
    locks them and keeps the approved payload as a packet artifact. A changed source or target invalidates it.
    """

    __tablename__ = "context_packet"
    __table_args__ = (
        CheckConstraint(_in("state", PACKET_STATES), name="state"),
        Index("ix_context_packet_task", "task_id", "created_at"),
    )
    packet_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"))
    target_session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    target_generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String)
    payload_blob: Mapped[str] = mapped_column(ForeignKey("blob.sha256"))
    payload_hash: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[str] = mapped_column(Text)  # JSON: sources, excerpts, redactions, flags, selection
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifact.artifact_id"))  # once confirmed
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ContextDelivery(Base):
    """A confirmed packet sent to its target. Created in the same transaction as its control action, whose
    state is the delivery state: acknowledged means the runtime accepted the input, not that it was used."""

    __tablename__ = "context_delivery"
    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    packet_id: Mapped[str] = mapped_column(ForeignKey("context_packet.packet_id"))
    action_id: Mapped[str] = mapped_column(ForeignKey("control_action.action_id"), unique=True)
    target_session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AgentCredential(Base):
    """The credential a managed session's tools use to call Robin (TR-066). Only its hash is stored. It
    identifies the session; it grants nothing beyond that session's retrieval grants and own artifacts."""

    __tablename__ = "agent_credential"
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RetrievalGrant(Base):
    """What one session may read (TR-068): an exact artifact version or an exact excerpt range, approved by
    the user. Grants never widen to newer versions or wider ranges, and can be revoked."""

    __tablename__ = "retrieval_grant"
    __table_args__ = (
        CheckConstraint(_in("kind", GRANT_KINDS), name="kind"),
        Index("ix_retrieval_grant_session", "session_id", "revoked_at"),
    )
    grant_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"))
    kind: Mapped[str] = mapped_column(String)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifact.artifact_id"))
    version: Mapped[int | None] = mapped_column(Integer)
    excerpt_session_id: Mapped[str | None] = mapped_column(ForeignKey("session.session_id"))
    after_cursor: Mapped[int | None] = mapped_column(Integer)
    through_cursor: Mapped[int | None] = mapped_column(Integer)
    approved_by: Mapped[str] = mapped_column(ForeignKey("audit_entry.audit_id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RetrievalRead(Base):
    """One read by an agent through Robin's tools: what it asked for and the exact content it got."""

    __tablename__ = "retrieval_read"
    __table_args__ = (Index("ix_retrieval_read_session", "session_id", "at"),)
    read_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    tool: Mapped[str] = mapped_column(String)
    request: Mapped[str] = mapped_column(Text)  # JSON
    grant_ids: Mapped[str] = mapped_column(Text)  # JSON list; empty for the session's own artifacts
    content_blob: Mapped[str] = mapped_column(ForeignKey("blob.sha256"))
    size: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(UTCDateTime)


class SearchDoc(Base):
    """Search index rows for things other than events (tasks, findings, syntheses, sessions). Event rows use
    their global cursor as the FTS rowid; these get negative rowids so the two never collide."""

    __tablename__ = "search_doc"
    doc_key: Mapped[str] = mapped_column(String, primary_key=True)  # "<kind>:<id>"
    fts_rowid: Mapped[int] = mapped_column(Integer, unique=True)
    indexed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PrunedEvent(Base):
    """An event whose payload was removed to stay under the storage cap (TR-081). The event row keeps its
    identity (source, sequence, content hash); its payload is a tombstone and it reads as unavailable."""

    __tablename__ = "pruned_event"
    global_cursor: Mapped[int] = mapped_column(ForeignKey("event.global_cursor"), primary_key=True)
    original_size: Mapped[int] = mapped_column(Integer)
    pruned_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RunGit(Base):
    """Git context of one run (FR-050, TR-075): the repository and base commit when it started, and what it
    changed by the end. Read in the background; a folder that isn't a repository has no row fields set."""

    __tablename__ = "run_git"
    run_id: Mapped[str] = mapped_column(ForeignKey("run.run_id"), primary_key=True)
    repo_root: Mapped[str | None] = mapped_column(String)
    branch: Mapped[str | None] = mapped_column(String)
    base_commit: Mapped[str | None] = mapped_column(String)
    start_dirty: Mapped[int | None] = mapped_column(Integer)  # changed or untracked files at the start
    start_snapshot: Mapped[str | None] = mapped_column(Text)  # JSON {path: blob hash or null}
    start_late: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # read after the run began
    start_captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_commit: Mapped[str | None] = mapped_column(String)
    end_dirty: Mapped[int | None] = mapped_column(Integer)
    changed_files: Mapped[str | None] = mapped_column(Text)  # JSON [{path, change}]
    shared_folder: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # others wrote there too
    truncated: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    end_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Worktree(Base):
    """A Git worktree Robin created for a session (FR-051, TR-072). Removing it is a separate confirmed
    action that never deletes its branch or commits; the row stays as the record."""

    __tablename__ = "worktree"
    worktree_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.task_id"))
    repo_root: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    branch: Mapped[str] = mapped_column(String)
    from_branch: Mapped[str | None] = mapped_column(String)  # what the repository had checked out
    base_commit: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SessionActivity(Base):
    """What a session is doing, derived from its events for the Map and rebuilt when the extractor or the
    redaction rules change. One row per lane: the main transcript (lane '') or one Claude subagent. Every
    string is redacted and truncated before it's stored."""

    __tablename__ = "session_activity"
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"), primary_key=True)
    lane: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String)  # a subagent's type, e.g. "Explore"
    tool: Mapped[str | None] = mapped_column(String)
    target: Mapped[str | None] = mapped_column(Text)
    action_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    turn_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_activity_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    todos: Mapped[str] = mapped_column(Text)  # JSON [{id, text, status}]
    todo_done: Mapped[int] = mapped_column(Integer)
    todo_total: Mapped[int] = mapped_column(Integer)
    files: Mapped[str] = mapped_column(Text)  # JSON [{path, op, at}], most recent first
    files_count: Mapped[int] = mapped_column(Integer)
    scratch: Mapped[str] = mapped_column(Text)  # JSON: TaskCreate calls waiting for their task id
