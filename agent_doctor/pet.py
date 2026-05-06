"""Doctor Pet state model for in-session Agent Doctor interventions.

The pet is a product surface, not a detector. It turns existing deterministic
findings and autopilot events into a compact status object that a CLI, MCP
host, menu-bar app, or future desktop widget can render consistently.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .autopilot import Action, AutopilotEvent, Platform, detect_autopilot_events
from .detectors import detect_findings
from .ingest import ingest_path_with_errors
from .redaction import redact_text, redact_value
from .schema import Finding, Message, Role, Severity

PetState = Literal["idle", "watching", "concerned", "intervening"]

_SEVERITY_RANK: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2}
_ACTION_RANK: dict[Action, int] = {"silent": 0, "notify": 1, "intervene": 2}


@dataclass(frozen=True)
class PetEvidence:
    file: str
    line: int
    role: Role
    quote: str

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "role": self.role,
            "quote": redact_text(self.quote),
        }


@dataclass(frozen=True)
class PetOption:
    id: str
    label: str
    description: str
    command: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "command": self.command,
        }


@dataclass(frozen=True)
class PetStatus:
    name: str
    persona: str
    state: PetState
    action: Action
    severity: Severity
    session_id: str
    headline: str
    message: str
    evidence: tuple[PetEvidence, ...]
    options: tuple[PetOption, ...]
    messages: int
    sessions: int
    findings: int
    events: int
    parse_errors: int = 0
    latest_event_id: str | None = None
    latest_trigger: str | None = None
    card_path: str | None = None
    finding_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "persona": self.persona,
            "state": self.state,
            "action": self.action,
            "severity": self.severity,
            "session_id": self.session_id,
            "headline": redact_text(self.headline),
            "message": redact_text(self.message),
            "evidence": [item.to_dict() for item in self.evidence],
            "options": [item.to_dict() for item in self.options],
            "messages": self.messages,
            "sessions": self.sessions,
            "findings": self.findings,
            "events": self.events,
            "parse_errors": self.parse_errors,
            "latest_event_id": self.latest_event_id,
            "latest_trigger": self.latest_trigger,
            "card_path": self.card_path,
            "finding_ids": list(self.finding_ids),
        }


def pet_status_for_path(
    path: Path,
    *,
    platform: Platform = "generic",
    strict: bool = False,
) -> PetStatus:
    messages, parse_errors = ingest_path_with_errors(path, strict=strict)
    findings = detect_findings(messages)
    return build_pet_status(
        messages,
        findings,
        platform=platform,
        parse_errors=parse_errors,
    )


def pet_status_for_text(
    text: str,
    *,
    platform: Platform = "generic",
    session_id: str = "manual",
) -> PetStatus:
    message = Message(
        file="<manual>",
        line=1,
        session_id=session_id,
        role="user",
        content=text,
        source_format=platform,
        raw_type="manual_pet_summon",
    )
    findings = detect_findings([message])
    return build_pet_status([message], findings, platform=platform)


def build_pet_status(
    messages: Iterable[Message],
    findings: Iterable[Finding] | None = None,
    *,
    platform: Platform = "generic",
    events: Iterable[AutopilotEvent] | None = None,
    parse_errors: int = 0,
) -> PetStatus:
    ordered = list(messages)
    detected = list(findings) if findings is not None else detect_findings(ordered)
    detected_events = (
        list(events)
        if events is not None
        else detect_autopilot_events(
            ordered,
            detected,
            platform=platform,
            min_severity="medium",
        )
    )
    sessions = len({message.session_id for message in ordered})

    if not ordered:
        return _idle_status(parse_errors=parse_errors)

    event = _select_event(detected_events)
    if event is not None:
        return _status_from_event(
            event,
            messages=len(ordered),
            sessions=sessions,
            findings=len(detected),
            events=len(detected_events),
            parse_errors=parse_errors,
        )

    finding = _select_finding(detected)
    if finding is not None:
        return _status_from_finding(
            finding,
            messages=len(ordered),
            sessions=sessions,
            findings=len(detected),
            parse_errors=parse_errors,
        )

    return PetStatus(
        name="Agent Doctor Pet",
        persona="doctor",
        state="idle",
        action="silent",
        severity="low",
        session_id=_latest_session_id(ordered),
        headline="No active quality incident detected.",
        message="The doctor is idle. Keep autopilot running to wake it when frustration or quality failures appear.",
        evidence=(),
        options=_idle_options(),
        messages=len(ordered),
        sessions=sessions,
        findings=0,
        events=0,
        parse_errors=parse_errors,
    )


def render_pet_markdown(status: PetStatus) -> str:
    lines = [
        "# Agent Doctor Pet",
        "",
        f"- State: `{status.state}`",
        f"- Action: `{status.action}`",
        f"- Severity: `{status.severity}`",
        f"- Session: `{status.session_id or 'n/a'}`",
        f"- Messages scanned: {status.messages}",
        f"- Findings: {status.findings}",
        f"- Events: {status.events}",
    ]
    if status.parse_errors:
        lines.append(f"- Skipped malformed lines: {status.parse_errors}")
    if status.latest_trigger:
        lines.append(f"- Trigger: `{status.latest_trigger}`")
    lines.extend(
        ["", "## Status", "", redact_text(status.headline), "", redact_text(status.message)]
    )
    if status.evidence:
        lines.extend(["", "## Evidence", ""])
        for item in status.evidence:
            lines.append(
                f"- `{item.file}:{item.line}` {item.role}: \"{redact_text(item.quote)}\""
            )
    if status.options:
        lines.extend(["", "## Options", ""])
        for option in status.options:
            suffix = f" Command: `{option.command}`" if option.command else ""
            lines.append(f"- `{option.id}` {option.label}: {option.description}{suffix}")
    lines.append("")
    return "\n".join(lines)


def write_pet_artifacts(out_dir: Path, status: PetStatus) -> dict[str, Path]:
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = out_dir / "pet-status.json"
    card_path = out_dir / "pet-card.md"
    payload = redact_value(status.to_dict())
    _write_private_text(
        status_path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    _write_private_text(card_path, render_pet_markdown(status))
    return {"status": status_path, "card": card_path}


def _idle_status(*, parse_errors: int = 0) -> PetStatus:
    return PetStatus(
        name="Agent Doctor Pet",
        persona="doctor",
        state="idle",
        action="silent",
        severity="low",
        session_id="",
        headline="No transcript messages scanned yet.",
        message="Point the pet at a JSONL transcript or pass a current user message to ask for help.",
        evidence=(),
        options=_idle_options(),
        messages=0,
        sessions=0,
        findings=0,
        events=0,
        parse_errors=parse_errors,
    )


def _status_from_event(
    event: AutopilotEvent,
    *,
    messages: int,
    sessions: int,
    findings: int,
    events: int,
    parse_errors: int,
) -> PetStatus:
    state: PetState = "intervening" if event.action == "intervene" else "concerned"
    evidence = (
        PetEvidence(
            file=event.message_file,
            line=event.message_line,
            role=_event_evidence_role(event),
            quote=event.evidence,
        ),
    )
    return PetStatus(
        name="Agent Doctor Pet",
        persona="doctor",
        state=state,
        action=event.action,
        severity=event.severity,
        session_id=event.session_id,
        headline=_event_headline(event),
        message=_event_message(event),
        evidence=evidence,
        options=_event_options(event),
        messages=messages,
        sessions=sessions,
        findings=findings,
        events=events,
        parse_errors=parse_errors,
        latest_event_id=event.id,
        latest_trigger=event.trigger,
        card_path=event.card_path,
        finding_ids=tuple(event.finding_ids),
    )


def _status_from_finding(
    finding: Finding,
    *,
    messages: int,
    sessions: int,
    findings: int,
    parse_errors: int,
) -> PetStatus:
    state: PetState = "concerned" if finding.severity == "high" else "watching"
    evidence = tuple(
        PetEvidence(item.file, item.line, item.role, item.quote)
        for item in finding.evidence[:3]
    )
    return PetStatus(
        name="Agent Doctor Pet",
        persona="doctor",
        state=state,
        action="notify",
        severity=finding.severity,
        session_id=finding.session_id,
        headline=f"{finding.title} detected.",
        message=(
            "The doctor found a durable quality pattern. Review the evidence, then stage "
            "patches if this should change future agent behavior."
        ),
        evidence=evidence,
        options=_finding_options(finding),
        messages=messages,
        sessions=sessions,
        findings=findings,
        events=0,
        parse_errors=parse_errors,
        finding_ids=(finding.id,),
    )


def _select_event(events: list[AutopilotEvent]) -> AutopilotEvent | None:
    if not events:
        return None
    return max(
        enumerate(events),
        key=lambda item: (
            _ACTION_RANK[item[1].action],
            _SEVERITY_RANK[item[1].severity],
            item[0],
        ),
    )[1]


def _select_finding(findings: list[Finding]) -> Finding | None:
    if not findings:
        return None
    return max(
        enumerate(findings),
        key=lambda item: (
            _SEVERITY_RANK[item[1].severity],
            item[1].count,
            item[0],
        ),
    )[1]


def _event_headline(event: AutopilotEvent) -> str:
    if event.action == "intervene":
        return "Doctor is intervening in a live quality incident."
    return "Doctor noticed a quality risk."


def _event_message(event: AutopilotEvent) -> str:
    if event.trigger == "user_frustration_signal":
        return (
            "User frustration or trust-break language is present. Pause the normal success path, "
            "name the concrete failure, cite evidence, and give one corrective next step."
        )
    if event.trigger == "completion_claim_without_nearby_verification":
        return (
            "A completion claim appears without nearby verification evidence. "
            "Verify before repeating success."
        )
    if event.trigger == "tool_failure_or_hidden_error":
        return (
            "A tool failure appears hidden or unacknowledged. Surface the error "
            "before claiming progress."
        )
    return event.summary


def _event_options(event: AutopilotEvent) -> tuple[PetOption, ...]:
    if event.message_file == "<manual>":
        stage_command = "agent-doctor scan --path <sessions> --out ./postmortem"
    else:
        stage_command = f"agent-doctor scan --path {event.message_file} --out ./postmortem"
    return (
        PetOption(
            id="pause_and_diagnose",
            label="Pause and diagnose",
            description="Stop the current success path and answer with evidence-backed recovery steps.",
        ),
        PetOption(
            id="stage_fix",
            label="Stage fix",
            description="Turn this incident into reviewable SOP, identity, or eval patches.",
            command=stage_command,
        ),
        PetOption(
            id="keep_watching",
            label="Keep watching",
            description="Leave the sidecar active and wait for the next deterministic trigger.",
        ),
    )


def _finding_options(finding: Finding) -> tuple[PetOption, ...]:
    return (
        PetOption(
            id="review_evidence",
            label="Review evidence",
            description="Inspect the transcript quotes before changing agent behavior.",
        ),
        PetOption(
            id="stage_fix",
            label="Stage fix",
            description="Stage reviewable patches for the detected finding.",
            command="agent-doctor apply --findings ./postmortem --out ./staging --min-severity medium",
        ),
        PetOption(
            id="generate_eval",
            label="Generate eval",
            description=f"Use `{finding.eval_case.get('name', 'eval_case')}` as a regression seed.",
        ),
    )


def _event_evidence_role(event: AutopilotEvent) -> Role:
    if event.trigger == "user_frustration_signal":
        return "user"
    if event.trigger == "tool_failure_or_hidden_error":
        return "tool"
    return "assistant"


def _idle_options() -> tuple[PetOption, ...]:
    return (
        PetOption(
            id="scan_session",
            label="Scan session",
            description="Point the doctor at JSONL transcripts to check for deterministic findings.",
            command="agent-doctor scan --path <sessions> --out ./postmortem",
        ),
        PetOption(
            id="start_autopilot",
            label="Start autopilot",
            description="Run the local sidecar so the pet wakes automatically on quality incidents.",
            command="agent-doctor setup autopilot",
        ),
    )


def _latest_session_id(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.session_id:
            return message.session_id
    return ""


def _write_private_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.chmod(path, 0o600)
