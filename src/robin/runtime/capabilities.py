"""What each runtime integration can do (FR-018, TR-011), from the Phase 0 capability matrix.

Controls a runtime doesn't support are disabled with a reason, never attempted. Discovery never implies
live control: an observed session is read-only regardless of what its runtime supports when managed.
"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Capabilities:
    runtime: str
    tested_versions: str
    launch: bool = True
    resume: bool = True
    new_turn: bool = True
    queued_followup: bool = True
    steer_mid_turn: bool = False  # True only where steering changes the running turn (Codex)
    interrupt: bool = True
    approvals: str = "native"  # native | robin_extension | none
    terminal_mode: str = "handoff"  # attach | handoff | unavailable
    finding_paths: tuple[str, ...] = ("tool", "message")
    review_independence: str = "enforced_with_deny_rules"  # enforced_with_deny_rules | not_isolated
    # How sessions started outside Robin are seen: opt-in hooks (`robin hooks install`) or transcripts only.
    observation: str = "hooks_or_transcripts"  # hooks_or_transcripts | transcripts_only
    level: str = "live"  # live | observed_only
    unsupported: dict[str, str] = field(default_factory=dict)

    def allows(self, action_type: str) -> tuple[bool, str | None]:
        needed = {
            "launch": self.launch,
            "message": self.new_turn,
            "interrupt": self.interrupt,
            "approval": self.approvals != "none",
            "stop": True,
            "handoff": self.terminal_mode != "unavailable",
            "reclaim": self.terminal_mode != "unavailable",
            "resume": self.resume,
        }.get(action_type, False)
        if self.level != "live":
            return (
                False,
                f"{self.runtime} is observed-only: {self.unsupported.get('live', 'no live control')}",
            )
        if not needed:
            return False, self.unsupported.get(action_type, f"{self.runtime} doesn't support {action_type}")
        return True, None

    def report(self) -> dict[str, Any]:
        return asdict(self)


RUNTIME_CAPABILITIES: dict[str, Capabilities] = {
    "claude": Capabilities("claude", "claude-agent-sdk 0.2.167 (bundled Claude Code 2.1.300)"),
    "codex": Capabilities("codex", "codex-cli 0.162.1", steer_mid_turn=True),
    "pi": Capabilities(
        "pi",
        "pi-coding-agent 1.2.0",
        approvals="robin_extension",
        review_independence="not_isolated",
        unsupported={"review_independence": "Pi has no sandbox"},
    ),
    "fake": Capabilities("fake", "test runtime"),
}


def capabilities_for(runtime: str) -> Capabilities:
    return RUNTIME_CAPABILITIES.get(runtime) or Capabilities(
        runtime,
        "unknown",
        launch=False,
        resume=False,
        new_turn=False,
        queued_followup=False,
        interrupt=False,
        approvals="none",
        terminal_mode="unavailable",
        finding_paths=(),
        review_independence="not_isolated",
        observation="transcripts_only",
        level="observed_only",
        unsupported={"live": f"no Robin adapter for {runtime}"},
    )
