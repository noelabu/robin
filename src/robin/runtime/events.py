"""The vocabulary runtime hosts speak, and its mapping to envelope event types (TR-030, TR-040).

Every runtime host emits these normalized event types alongside whatever native detail it adds.
Control acknowledgements carry the action_id they answer, which is how the ledger correlates them.
"""

from robin.events.envelope import EvidenceKind
from robin.events.supervisor_source import MappedSupervisorEvent, SupervisorRecord

# host event type -> (envelope event type, evidence kind)
HOST_EVENTS: dict[str, tuple[str, EvidenceKind]] = {
    "host_spawned": ("connection.connected", EvidenceKind.OBSERVED),
    "host_exited": ("session.stopped", EvidenceKind.OBSERVED),
    "session_started": ("session.started", EvidenceKind.REPORTED),
    "session_stopping": ("session.stopped", EvidenceKind.REPORTED),
    "turn_started": ("session.turn_started", EvidenceKind.REPORTED),
    "turn_completed": ("session.turn_completed", EvidenceKind.REPORTED),
    "turn_interrupted": ("session.turn_completed", EvidenceKind.REPORTED),
    "turn_failed": ("session.failed", EvidenceKind.REPORTED),
    "message_delta": ("message.delta", EvidenceKind.REPORTED),
    "message_completed": ("message.completed", EvidenceKind.REPORTED),
    "user_message": ("message.user", EvidenceKind.REPORTED),
    "tool_started": ("tool.started", EvidenceKind.REPORTED),
    "tool_output": ("tool.output", EvidenceKind.REPORTED),
    "tool_result": ("tool.result", EvidenceKind.REPORTED),
    "tool_error": ("tool.error", EvidenceKind.REPORTED),
    "file_changed": ("file.change", EvidenceKind.REPORTED),
    "plan_updated": ("session.plan_updated", EvidenceKind.REPORTED),
    "approval_request": ("attention.approval_requested", EvidenceKind.REPORTED),
    "question": ("attention.question", EvidenceKind.REPORTED),
    "approval_expired": ("attention.expired", EvidenceKind.OBSERVED),
    "input_submitted": ("control.acknowledged", EvidenceKind.REPORTED),
    "interrupt_acknowledged": ("control.acknowledged", EvidenceKind.REPORTED),
    "approval_answered": ("control.acknowledged", EvidenceKind.REPORTED),
    "approval_rejected": ("control.rejected", EvidenceKind.REPORTED),
    "command_rejected": ("control.rejected", EvidenceKind.REPORTED),
    "command_failed": ("control.rejected", EvidenceKind.REPORTED),
    "finding_submission": ("finding.published", EvidenceKind.REPORTED),
}


def host_mapper(record: SupervisorRecord) -> MappedSupervisorEvent:
    kind = record.event.get("type")
    event_type, evidence = HOST_EVENTS.get(str(kind), ("unknown.raw", EvidenceKind.UNKNOWN))
    return MappedSupervisorEvent(
        event_type=event_type,
        evidence_kind=evidence,
        native_session_id=record.event.get("native_session_id"),
        native_event_id=record.event.get("native_event_id"),
    )
