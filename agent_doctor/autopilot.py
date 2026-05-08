"""Autopilot sidecar for platform-agnostic agent quality monitoring.

The sidecar deliberately stays outside host runtimes. It reads existing
transcripts/log-shaped JSONL through the same ingestion boundary as ``scan``,
keeps its own SQLite state for de-duplication, and writes diagnosis cards to
an Agent Doctor output directory. No OpenClaw/Hermes source changes or runtime
hooks are required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Literal

from .detectors import detect_findings
from .frustration import classify_user_frustration
from .ingest import (
    DEFAULT_HERMES_PATH,
    DEFAULT_OPENCLAW_PATH,
    collect_jsonl_paths,
    ingest_file_range_with_errors,
    ingest_path_with_errors,
)
from .redaction import redact_text, redact_value
from .schema import Evidence, Finding, Message, Severity

Platform = Literal["openclaw", "hermes", "generic"]
Action = Literal["silent", "notify", "intervene"]

# Detection regexes intentionally live in `agent_doctor.detectors` — the scan
# and autopilot paths share the same source of truth so a regex change in one
# place can never silently diverge from the other. Older imports of
# `COMPLETION_CLAIM` / `VERIFYING_ACTION` from this module have been removed.

SEVERITY_RANK: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class AutopilotEvent:
    id: str
    platform: Platform
    action: Action
    trigger: str
    severity: Severity
    session_id: str
    message_file: str
    message_line: int
    summary: str
    evidence: str
    finding_ids: list[str]
    card_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AutopilotResult:
    platform: Platform
    input_path: str
    out_dir: str
    messages: int
    sessions: int
    findings: int
    parse_errors: int
    events: list[AutopilotEvent]
    suppressed: int = 0
    delivery_errors: list[str] | None = None
    pet_state: str = "idle"
    pet_status_path: str | None = None
    pet_card_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["events"] = [event.to_dict() for event in self.events]
        return data


@dataclass(frozen=True)
class JsonlChange:
    path: Path
    start_byte: int = 0
    line_offset: int = 0


def default_transcript_path(platform: Platform) -> Path:
    if platform == "openclaw":
        return DEFAULT_OPENCLAW_PATH
    if platform == "hermes":
        return DEFAULT_HERMES_PATH
    raise ValueError("generic autopilot requires --path")


def run_autopilot_once(
    *,
    platform: Platform,
    out_dir: Path,
    path: Path | None = None,
    state_path: Path | None = None,
    cooldown_seconds: int = 3600,
    min_severity: Severity = "medium",
    notify_command: str | None = None,
    inbox_dir: Path | None = None,
    pet_out_dir: Path | None = None,
    dispatch_adapter: bool = False,
    changed_only: bool = False,
) -> AutopilotResult:
    input_path = path or default_transcript_path(platform)
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = AutopilotState(state_path or out_dir / "state.sqlite3")
    changed_paths: list[Path] | None = None
    snapshot_paths: list[Path] | None = None
    try:
        if changed_only:
            first_platform_scan = (
                platform in {"openclaw", "hermes"}
                and input_path.expanduser().is_dir()
                and not state.has_file_snapshots()
            )
            if first_platform_scan:
                changed_paths = state.changed_jsonl_paths(input_path)
                snapshot_paths = changed_paths
                changed_paths = _latest_session_paths(changed_paths)
                messages, parse_errors = _ingest_paths(changed_paths)
            else:
                changes = state.changed_jsonl_changes(input_path)
                changed_paths = [change.path for change in changes]
                messages, parse_errors = _ingest_changes(changes)
        else:
            messages, parse_errors = ingest_path_with_errors(input_path)
            changed_paths = collect_jsonl_paths(input_path)
        state.record_file_snapshots(snapshot_paths or changed_paths)
    except Exception:
        state.close()
        raise
    findings = detect_findings(messages)
    candidates = detect_autopilot_events(
        messages,
        findings,
        platform=platform,
        min_severity=min_severity,
    )

    emitted: list[AutopilotEvent] = []
    delivery_errors: list[str] = []
    dismissed_event_ids: set[str] = set()
    dismissed_finding_ids: set[str] = set()
    suppressed = 0
    try:
        for event in candidates:
            if state.should_emit(event, cooldown_seconds=cooldown_seconds):
                event = write_diagnosis_card(out_dir, event, findings)
                append_event(out_dir / "events.jsonl", event)
                write_regression_eval(out_dir / "regressions", event)
                if inbox_dir is not None:
                    write_inbox_advisory(inbox_dir, event)
                delivered = True
                if notify_command:
                    error = run_notify_command(notify_command, event)
                    if error:
                        delivered = False
                        delivery_errors.append(error)
                        append_delivery_error(out_dir / "delivery-errors.jsonl", event, error)
                if dispatch_adapter:
                    adapter_error = _dispatch_via_adapter(event, platform=platform)
                    if adapter_error:
                        # Adapter-side errors don't block delivered status: legacy notify_command
                        # is the gate. Adapter dispatch is best-effort additive.
                        delivery_errors.append(adapter_error)
                        append_delivery_error(out_dir / "delivery-errors.jsonl", event, adapter_error)
                if delivered:
                    state.record(event)
                emitted.append(event)
            else:
                if state.is_dismissed(event):
                    dismissed_event_ids.add(event.id)
                    dismissed_finding_ids.update(event.finding_ids)
                suppressed += 1
    finally:
        state.close()

    pet_status_path: str | None = None
    pet_card_path: str | None = None
    pet_state = "idle"
    try:
        from .pet import build_pet_status, write_pet_artifacts

        event_overrides = {event.id: event for event in emitted}
        pet_events = [
            event_overrides.get(event.id, event)
            for event in candidates
            if event.id not in dismissed_event_ids
        ]
        visible_findings = [
            finding for finding in findings if finding.id not in dismissed_finding_ids
        ]
        pet_status = build_pet_status(
            messages,
            visible_findings,
            platform=platform,
            events=pet_events,
            parse_errors=parse_errors,
        )
        pet_status = replace(pet_status, dismiss_state_path=str(state.path))
        preserve_dir = pet_out_dir.expanduser() if pet_out_dir is not None else out_dir
        preserved = _preserved_active_pet_paths(preserve_dir, pet_status, messages)
        if preserved is not None:
            pet_state = preserved["state"]
            pet_status_path = str(preserved["status"])
            pet_card_path = str(preserved["card"])
        else:
            pet_paths = write_pet_artifacts(out_dir, pet_status)
            if pet_out_dir is not None and pet_out_dir.expanduser() != out_dir:
                write_pet_artifacts(pet_out_dir, pet_status)
            pet_state = pet_status.state
            pet_status_path = str(pet_paths["status"])
            pet_card_path = str(pet_paths["card"])
    except OSError as exc:
        delivery_errors.append(f"pet_status_write_failed: {exc}")

    # v1 boundary: the desktop Doctor/pet can tell the current OpenClaw agent
    # how to recover, but Agent Doctor must not auto-apply config/SOP/memory or
    # run reaction-approval loops from autopilot. Durable changes remain
    # reviewable via explicit scan/apply flows.

    return AutopilotResult(
        platform=platform,
        input_path=str(input_path.expanduser()),
        out_dir=str(out_dir),
        messages=len(messages),
        sessions=len({message.session_id for message in messages}),
        findings=len(findings),
        parse_errors=parse_errors,
        events=emitted,
        suppressed=suppressed,
        delivery_errors=delivery_errors,
        pet_state=pet_state,
        pet_status_path=pet_status_path,
        pet_card_path=pet_card_path,
    )


def baseline_autopilot_state(
    *,
    platform: Platform,
    out_dir: Path,
    path: Path | None = None,
    state_path: Path | None = None,
) -> int:
    """Record current JSONL snapshots without emitting historical events."""

    input_path = path or default_transcript_path(platform)
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = collect_jsonl_paths(input_path)
    state = AutopilotState(state_path or out_dir / "state.sqlite3")
    try:
        state.record_file_snapshots(paths)
    finally:
        state.close()
    return len(paths)


def _preserved_active_pet_paths(
    out_dir: Path,
    next_status,
    messages: list[Message],
) -> dict[str, object] | None:
    """Keep a live pet intervention visible across empty changed-only polls.

    The watch loop often sees an incident on one cycle, then zero new JSONL
    lines two seconds later. That empty cycle should not immediately overwrite
    an active desktop Doctor card with idle; the desktop surface already has an
    expiry window for live incidents.
    """

    if messages or next_status.state != "idle":
        return None
    status_path = out_dir.expanduser() / "pet-status.json"
    card_path = out_dir.expanduser() / "pet-card.md"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        state = str(payload.get("state", "idle"))
        expires_after = float(payload.get("expires_after_seconds", 0) or 0)
        age = time.time() - status_path.stat().st_mtime
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if state == "idle" or expires_after <= 0 or age >= expires_after:
        return None
    return {"state": state, "status": status_path, "card": card_path}


def _ingest_paths(paths: list[Path]) -> tuple[list[Message], int]:
    messages: list[Message] = []
    parse_errors = 0
    for path in paths:
        file_messages, file_errors = ingest_path_with_errors(path)
        messages.extend(file_messages)
        parse_errors += file_errors
    return messages, parse_errors


def _ingest_changes(changes: list[JsonlChange]) -> tuple[list[Message], int]:
    messages: list[Message] = []
    parse_errors = 0
    for change in changes:
        file_messages, file_errors = ingest_file_range_with_errors(
            change.path,
            start_byte=change.start_byte,
            line_offset=change.line_offset,
        )
        messages.extend(file_messages)
        parse_errors += file_errors
    return messages, parse_errors


def _latest_session_paths(paths: list[Path], *, limit: int = 3) -> list[Path]:
    ordinary = [path for path in paths if not path.name.endswith(".trajectory.jsonl")]
    selected = ordinary or paths
    return sorted(selected, key=lambda path: path.stat().st_mtime_ns, reverse=True)[:limit]


def detect_autopilot_events(
    messages: Iterable[Message],
    findings: Iterable[Finding],
    *,
    platform: Platform,
    min_severity: Severity = "medium",
) -> list[AutopilotEvent]:
    """Derive autopilot events from already-computed findings.

    The autopilot deliberately does NOT re-run any per-turn regex matching
    here. Every trigger maps to a detector failure mode, and the detector
    is the single source of truth. Re-running the regex inside the autopilot
    is what allowed the scan and autopilot paths to diverge before
    (a `COMPLETION_CLAIM` regex local to autopilot drifted from the
    detector-side `COMPLETION_CLAIM_PATTERN`). Iterating findings prevents
    that class of bug entirely.

    The ``messages`` argument is retained for backwards compatibility and so
    callers (and tests) can still pass the full transcript stream; it is not
    used for detection.
    """

    del messages  # detection now lives entirely in detect_findings

    events: list[AutopilotEvent] = []
    for finding in findings:
        event = _event_from_finding(finding, platform=platform)
        if event is not None:
            events.append(event)

    return [
        event
        for event in events
        if SEVERITY_RANK[event.severity] >= SEVERITY_RANK[min_severity]
    ]


def _event_from_finding(finding: Finding, *, platform: Platform) -> AutopilotEvent | None:
    """Map a single Finding to its autopilot event, or None if not a trigger."""

    mode = finding.failure_mode
    if mode == "user_frustration_signal":
        primary = _first_evidence_for_role(finding, "user") or finding.evidence[0]
        # Re-classify the redacted/normalized evidence quote so the
        # autopilot summary preserves the labeled rationale (e.g.
        # "trust_degradation, direct_quality_complaint"). The classifier is
        # the same one used by the scan path, so this cannot diverge.
        signal = classify_user_frustration(primary.quote)
        action: Action = "intervene" if finding.severity == "high" else "notify"
        return _build_event(
            platform=platform,
            trigger="user_frustration_signal",
            severity=finding.severity,
            message=_message_from_evidence(primary, finding.session_id),
            summary=_frustration_summary_from_signal(signal, finding),
            evidence=primary.quote,
            findings=[finding],
            action=action,
        )
    if mode == "unsupported_completion_claim":
        primary = _first_evidence_for_role(finding, "assistant") or finding.evidence[0]
        return _build_event(
            platform=platform,
            trigger="completion_claim_without_nearby_verification",
            severity=finding.severity,
            message=_message_from_evidence(primary, finding.session_id),
            summary="Assistant appears to claim completion without nearby verification evidence.",
            evidence=primary.quote,
            findings=[finding],
            action="notify",
        )
    if mode == "tool_failure_or_hidden_error":
        primary = finding.evidence[0]
        return _build_event(
            platform=platform,
            trigger="tool_failure_or_hidden_error",
            severity=finding.severity,
            message=_message_from_evidence(primary, finding.session_id),
            summary="Tool failure was hidden or not acknowledged by the assistant.",
            evidence=primary.quote if finding.evidence else finding.diagnosis,
            findings=[finding],
            action="notify",
        )
    if mode == "trust_degradation_episode":
        primary = finding.evidence[0]
        return _build_event(
            platform=platform,
            trigger="trust_degradation_episode",
            severity="high",
            message=_message_from_evidence(primary, finding.session_id),
            summary=_trust_episode_summary(finding),
            evidence=_trust_episode_evidence_blob(finding),
            findings=[finding],
            action="intervene",
        )
    return None


def _first_evidence_for_role(finding: Finding, role: str) -> Evidence | None:
    for item in finding.evidence:
        if item.role == role:
            return item
    return None


def _message_from_evidence(evidence: Evidence, session_id: str) -> Message:
    return Message(
        file=evidence.file,
        line=evidence.line,
        session_id=session_id,
        role=evidence.role,
        content=evidence.quote,
    )


def _trust_episode_summary(finding: Finding) -> str:
    quote_count = len(finding.evidence)
    return (
        f"Trust-degradation episode: {quote_count} trust-eroding signal"
        f"{'s' if quote_count != 1 else ''} clustered in one session. "
        "Treat this as a recovery moment — pause normal execution, summarize the "
        "pattern across recent turns, and require user acknowledgement before resuming."
    )


def _trust_episode_evidence_blob(finding: Finding) -> str:
    snippets: list[str] = []
    for item in finding.evidence[:6]:
        snippets.append(f"[{item.role}] {item.quote}")
    return "\n".join(snippets) or finding.diagnosis


def write_diagnosis_card(
    out_dir: Path, event: AutopilotEvent, findings: Iterable[Finding]
) -> AutopilotEvent:
    cards_dir = out_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    card_path = cards_dir / f"{event.id}.md"
    related = [finding for finding in findings if finding.id in event.finding_ids]
    lines = [
        "# Agent Doctor Autopilot",
        "",
        f"- Trigger: `{event.trigger}`",
        f"- Severity: `{event.severity}`",
        f"- Action: `{event.action}`",
        f"- Platform: `{event.platform}`",
        f"- Session: `{event.session_id}`",
        f"- Evidence: `{event.message_file}:{event.message_line}`",
        "",
        "## Diagnosis",
        "",
        event.summary,
        "",
        "## Evidence",
        "",
        f"> {_card_evidence(event)}",
        "",
        "## Recommended Action",
        "",
        _recommend_action(event),
    ]
    if event.action == "intervene":
        lines.extend(_intervention_instruction_lines(event))
    if related:
        lines.extend(["", "## Related Findings", ""])
        for finding in related[:5]:
            lines.append(
                f"- `{finding.id}` `{finding.failure_mode}` {finding.severity}: {finding.title}"
            )
    lines.append("")
    _write_private_text(card_path, "\n".join(lines))
    latest_path = out_dir / "latest.md"
    _write_private_text(latest_path, "\n".join(lines))
    return replace(event, card_path=str(card_path))


def _card_evidence(event: AutopilotEvent) -> str:
    evidence = event.evidence.strip()
    try:
        payload = json.loads(evidence)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        content_items = payload.get("contentItems")
        if isinstance(content_items, list):
            parts = [
                str(item.get("text") or item.get("content") or "").strip()
                for item in content_items
                if isinstance(item, dict)
            ]
            evidence = "\n".join(part for part in parts if part) or evidence
        else:
            for key in ("text", "content", "message", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    evidence = value.strip()
                    break
    evidence = re.sub(
        r"(Conversation info|Sender) \(untrusted metadata\):\s*```json.*?```\s*",
        "",
        evidence,
        flags=re.S,
    )
    evidence = re.sub(r"\s+", " ", evidence).strip()
    if evidence.startswith("{") and evidence.endswith("}"):
        evidence = "Structured tool output was omitted from the user-facing card."
    return redact_text(evidence)[:1200]


def _intervention_instruction_lines(event: AutopilotEvent) -> list[str]:
    """Return the recovery + acknowledgement instructions for intervene cards.

    For trust-degradation episodes the acknowledgement requirement is explicit:
    the agent must surface the pattern AND wait for the user to acknowledge
    before proceeding. For other intervene triggers we keep the shorter
    recovery instruction so we don't over-shape every situation as an episode.
    """

    base = [
        "",
        "## Immediate Agent Instruction",
        "",
        "Pause the normal success path. Answer the user with a concise recovery response: "
        "name the concrete failure, cite the evidence you used, and state the next corrective action. "
        "Do not defend the prior response or write a long apology.",
    ]
    if event.trigger != "trust_degradation_episode":
        return base
    base.extend(
        [
            "",
            "## Required Acknowledgement",
            "",
            "This is a high-confidence trust-degradation episode (multiple frustration / correction "
            "signals in one session). Before doing any further task work, you MUST:",
            "",
            "1. Explicitly acknowledge the cumulative pattern, not just the latest message.",
            "2. List the concrete recovery actions you are about to take.",
            "3. Ask the user to confirm the recovery plan and only resume after acknowledgement.",
            "",
            "Treat this section as a hard precondition, not advice. The advisory file in the inbox "
            "must remain visible until the user has acknowledged.",
        ]
    )
    return base


def append_event(path: Path, event: AutopilotEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(redact_value(event.to_dict()), ensure_ascii=False)
    # The 0o600 mode arg on os.open is honored when the file is created; on
    # subsequent appends the file already exists at 0o600 from the first run,
    # and the parent dir is 0o700, so a follow-up chmod is redundant.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def write_inbox_advisory(inbox_dir: Path, event: AutopilotEvent) -> Path:
    inbox_dir = inbox_dir.expanduser()
    inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "-", event.session_id)[:120] or "session"
    path = inbox_dir / f"{safe_session}.md"
    lines = [
        "# Agent Doctor Advisory",
        "",
        f"- Trigger: `{event.trigger}`",
        f"- Severity: `{event.severity}`",
        f"- Action: `{event.action}`",
        f"- Card: `{event.card_path or ''}`",
        "",
        event.summary,
        "",
        "This advisory was generated by the outside-in Agent Doctor sidecar. "
        "Use it as evidence to adjust the current response; do not treat it as a live config patch.",
        "",
    ]
    if event.trigger == "trust_degradation_episode":
        lines.extend(
            [
                "## Acknowledgement Required",
                "",
                "Do not resume normal task work until you have:",
                "",
                "- Surfaced the cumulative trust-loss pattern to the user.",
                "- Stated a concrete recovery plan grounded in the evidence.",
                "- Received an explicit acknowledgement / go-ahead.",
                "",
                "Leave this advisory in place until the user acknowledges; the host agent "
                "should re-read it before each subsequent response in this session.",
                "",
            ]
        )
    _write_private_text(path, "\n".join(lines))
    return path


def run_notify_command(command: str, event: AutopilotEvent) -> str | None:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"invalid notify command: {exc}"
    if not args:
        return "empty notify command"
    env = os.environ.copy()
    env.update(
        {
            "AGENT_DOCTOR_EVENT_ID": event.id,
            "AGENT_DOCTOR_TRIGGER": event.trigger,
            "AGENT_DOCTOR_SEVERITY": event.severity,
            "AGENT_DOCTOR_ACTION": event.action,
            "AGENT_DOCTOR_SESSION_ID": event.session_id,
            "AGENT_DOCTOR_CARD": event.card_path or "",
            "AGENT_DOCTOR_SUMMARY": event.summary,
        }
    )
    try:
        completed = subprocess.run(
            args,
            check=False,  # we want to inspect the result, not raise
            text=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return f"timeout after {exc.timeout}s: {' '.join(args)}"
    except OSError as exc:
        return f"could not start notify command {args!r}: {exc}"
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip() if completed.stderr else ""
        stdout_tail = completed.stdout.strip() if completed.stdout else ""
        parts = [f"rc={completed.returncode}"]
        if stderr_tail:
            parts.append(f"stderr={stderr_tail!r}")
        if stdout_tail:
            parts.append(f"stdout={stdout_tail!r}")
        return " ".join(parts)
    return None


def dispatch_event(
    event: AutopilotEvent,
    adapter,  # HostAdapter; not type-hinted to avoid circular import
    *,
    target_resolver=None,
) -> str | None:
    """Adapter-driven event delivery. Parallel to run_notify_command.

    Phase 1 introduces this so Phase 3 has a foundation; the existing
    --notify-command path remains for backward compatibility.

    Returns None on success, an error string on failure.
    """
    from .adapters import MessageBody, MessageKind, Target  # late import to avoid circular

    caps = adapter.capabilities()
    kind = MessageKind.intervene  # phase 1: only intervene events

    if target_resolver is not None:
        target = target_resolver(event)
    else:
        # Default: inbox fallback under the agent-doctor output for this host
        inbox_path = (
            Path("~/.agent-doctor").expanduser()
            / caps.host_name
            / "inbox"
            / f"{event.session_id}.md"
        )
        target = Target(
            host=caps.host_name,
            channel="inbox",
            recipient="",
            inbox_path=inbox_path,
        )

    summary = event.summary or (event.evidence[:400] if event.evidence else "")
    body = MessageBody(
        header=f"🩺 Agent Doctor — {event.trigger}",
        body=summary if summary else "(no summary)",
        footer=f"Card: {event.card_path or 'n/a'}",
    )
    try:
        adapter.send_message(target, body, kind)
    except (NotImplementedError, RuntimeError) as exc:
        return f"adapter_error: {exc}"
    return None


def _dispatch_via_adapter(event: AutopilotEvent, *, platform: Platform) -> str | None:
    """Try to deliver the event through the host adapter's send_message.

    Best-effort: returns an error string on failure (logged to
    delivery-errors.jsonl) but never raises. Phase 3 of the redesign
    introduces this; the legacy --notify-command path is unchanged.
    """
    try:
        from .adapters import GenericAdapter, HermesAdapter, MessageKind, OpenClawAdapter
        from .channel_router import resolve
        from .speaker import render_intervene
    except ImportError as exc:
        return f"adapter_dispatch_import_failed: {exc}"

    adapter_classes = {
        "openclaw": OpenClawAdapter,
        "hermes": HermesAdapter,
        "generic": GenericAdapter,
    }
    cls = adapter_classes.get(platform, GenericAdapter)
    instance = cls.detect()
    if instance is None:
        instance = GenericAdapter()  # always-available fallback

    try:
        target, language = resolve(Path(event.message_file), instance)
    except Exception as exc:
        return f"channel_router_failed: {exc}"

    body = render_intervene(event, language=language)
    try:
        instance.send_message(target, body, MessageKind.intervene)
    except (NotImplementedError, RuntimeError) as exc:
        return f"adapter_dispatch_failed: {exc}"
    return None


def _apply_and_log(proposal, adapter, out_dir) -> bool:
    """Apply a proposal and log the result to patch-log.jsonl on success.

    Returns True iff state == 'applied'.
    """
    from .applier import apply_proposal as _apply
    result = _apply(proposal, adapter)
    if result.state == "applied":
        _append_patch_log(result, proposal)
        return True
    return False


def _append_patch_log(applied_patch, proposal) -> None:
    """Append a row to ~/.agent-doctor/patch-log.jsonl describing this apply."""
    log_path = Path("~/.agent-doctor").expanduser() / "patch-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "id": applied_patch.patch_id,
        "target_file": str(applied_patch.target_file),
        "backup_path": str(applied_patch.backup_path) if applied_patch.backup_path else None,
        "applied_at": time.time(),
        "session_id": proposal.session_id,
        "target_kind": proposal.target_kind,
        "undo_command": f"agent-doctor undo {applied_patch.patch_id}",
    }
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as h:
            h.write(json.dumps(payload, ensure_ascii=False) + "\n")
    finally:
        os.chmod(log_path, 0o600)


def _persist_proposal_transitions(path: Path, transitions) -> None:
    """Rewrite proposals.jsonl with new states from transitions."""
    from .proposer import load_proposals
    from dataclasses import replace as _dc_replace

    by_id = {t.proposal_id: t for t in transitions}
    proposals = load_proposals(path)
    new_lines: list[str] = []
    for p in proposals:
        if p.id in by_id:
            t = by_id[p.id]
            p = _dc_replace(p, state=t.new_state, resolved_at=time.time())
        new_lines.append(json.dumps(p.to_dict(), ensure_ascii=False))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write("\n".join(new_lines) + ("\n" if new_lines else ""))
    finally:
        os.chmod(path, 0o600)


def _replace_proposals_with_redrafts(path: Path, redrafts) -> None:
    """Replace each redrafted proposal in proposals.jsonl by id."""
    from .proposer import load_proposals

    redraft_by_id = {p.id: p for p in redrafts}
    proposals = load_proposals(path)
    new_lines: list[str] = []
    for p in proposals:
        if p.id in redraft_by_id:
            p = redraft_by_id[p.id]
        new_lines.append(json.dumps(p.to_dict(), ensure_ascii=False))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write("\n".join(new_lines) + ("\n" if new_lines else ""))
    finally:
        os.chmod(path, 0o600)


REGRESSION_ELIGIBLE_TRIGGERS: frozenset[str] = frozenset(
    {"user_frustration_signal", "trust_degradation_episode"}
)


def write_regression_eval(regressions_dir: Path, event: AutopilotEvent) -> Path | None:
    """Persist a regression eval case for missed user-frustration phrases.

    Each regression entry is a JSONL row pinned to the user-quote that should
    *always* trip the detector. Re-runs of the eval harness (or future
    detector edits) can replay these phrases to ensure phrases like
    ``你最近怎么越来越笨了`` are not silently regressed out of the classifier.
    """

    if event.trigger not in REGRESSION_ELIGIBLE_TRIGGERS:
        return None
    if event.severity != "high":
        return None
    regressions_dir = regressions_dir.expanduser()
    regressions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = regressions_dir / "frustration-regressions.jsonl"
    quote = redact_text(event.evidence).strip()
    if len(quote) > 600:
        quote = quote[:597].rstrip() + "..."
    payload = {
        "id": event.id,
        "trigger": event.trigger,
        "platform": event.platform,
        "session_id": event.session_id,
        "expected_match": True,
        "expected_severity": "high",
        "expected_modes": _expected_modes_for(event.trigger),
        "phrase": quote,
        "summary": event.summary,
    }
    # 0o600 is honored on creation; subsequent appends find the file already
    # at 0o600 from the first run. No follow-up chmod needed.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def _expected_modes_for(trigger: str) -> list[str]:
    if trigger == "trust_degradation_episode":
        return ["trust_degradation_episode", "user_frustration_signal"]
    if trigger == "user_frustration_signal":
        return ["user_frustration_signal"]
    return [trigger]


def append_delivery_error(path: Path, event: AutopilotEvent, error: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "event_id": event.id,
        "trigger": event.trigger,
        "session_id": event.session_id,
        "error": redact_text(error),
    }
    # 0o600 honored on creation; subsequent appends find the existing file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class AutopilotState:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS emitted_events (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              trigger TEXT NOT NULL,
              emitted_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dismissed_events (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              trigger TEXT NOT NULL,
              dismissed_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_files (
              path TEXT PRIMARY KEY,
              mtime_ns INTEGER NOT NULL,
              size INTEGER NOT NULL
            )
            """
        )
        self.connection.commit()
        # SQLite creates the database file via its own open() that does NOT
        # honor our umask the way os.open(..., 0o600) does, so the SQLite
        # file can land at 0o644 / 0o664 on common platforms. The chmod here
        # is a deliberate defense-in-depth — not redundant — to keep the
        # local-only privacy guarantee intact.
        try:
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            pass

    def should_emit(self, event: AutopilotEvent, *, cooldown_seconds: int) -> bool:
        if self.is_dismissed(event):
            return False
        row = self.connection.execute(
            "SELECT emitted_at FROM emitted_events WHERE id = ?", (event.id,)
        ).fetchone()
        if row is not None:
            return False
        row = self.connection.execute(
            """
            SELECT emitted_at FROM emitted_events
            WHERE session_id = ? AND trigger = ?
            ORDER BY emitted_at DESC LIMIT 1
            """,
            (event.session_id, event.trigger),
        ).fetchone()
        if row is None:
            return True
        return (time.time() - float(row[0])) >= cooldown_seconds

    def is_dismissed(self, event: AutopilotEvent) -> bool:
        row = self.connection.execute(
            "SELECT dismissed_at FROM dismissed_events WHERE id = ?", (event.id,)
        ).fetchone()
        return row is not None

    def record(self, event: AutopilotEvent) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO emitted_events(id, session_id, trigger, emitted_at)
            VALUES (?, ?, ?, ?)
            """,
            (event.id, event.session_id, event.trigger, time.time()),
        )
        self.connection.commit()

    def dismiss(self, event_id: str, session_id: str, trigger: str) -> None:
        if not event_id:
            return
        self.connection.execute(
            """
            INSERT INTO dismissed_events(id, session_id, trigger, dismissed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              session_id = excluded.session_id,
              trigger = excluded.trigger,
              dismissed_at = excluded.dismissed_at
            """,
            (event_id, session_id, trigger, time.time()),
        )
        self.connection.commit()

    def changed_jsonl_paths(self, root: Path) -> list[Path]:
        paths = collect_jsonl_paths(root)
        changed: list[Path] = []
        for path in paths:
            stat_result = path.stat()
            row = self.connection.execute(
                "SELECT mtime_ns, size FROM seen_files WHERE path = ?", (str(path),)
            ).fetchone()
            if row is None or int(row[0]) != stat_result.st_mtime_ns or int(row[1]) != stat_result.st_size:
                changed.append(path)
        return changed

    def changed_jsonl_changes(self, root: Path) -> list[JsonlChange]:
        paths = collect_jsonl_paths(root)
        changes: list[JsonlChange] = []
        for path in paths:
            stat_result = path.stat()
            row = self.connection.execute(
                "SELECT mtime_ns, size FROM seen_files WHERE path = ?", (str(path),)
            ).fetchone()
            if row is None:
                changes.append(JsonlChange(path=path))
                continue
            old_mtime_ns, old_size = int(row[0]), int(row[1])
            if old_mtime_ns == stat_result.st_mtime_ns and old_size == stat_result.st_size:
                continue
            if stat_result.st_size <= old_size:
                # Rotation/truncation/rewrite: rescan the new file content.
                changes.append(JsonlChange(path=path))
                continue
            changes.append(
                JsonlChange(
                    path=path,
                    start_byte=old_size,
                    line_offset=_count_lines_before_byte(path, old_size),
                )
            )
        return changes

    def has_file_snapshots(self) -> bool:
        row = self.connection.execute("SELECT 1 FROM seen_files LIMIT 1").fetchone()
        return row is not None

    def record_file_snapshots(self, paths: list[Path]) -> None:
        for path in paths:
            stat_result = path.stat()
            self.connection.execute(
                """
                INSERT INTO seen_files(path, mtime_ns, size)
                VALUES (?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  mtime_ns = excluded.mtime_ns,
                  size = excluded.size
                """,
                (str(path), stat_result.st_mtime_ns, stat_result.st_size),
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def _count_lines_before_byte(path: Path, byte_offset: int) -> int:
    if byte_offset <= 0:
        return 0
    count = 0
    remaining = byte_offset
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            count += chunk.count(b"\n")
            remaining -= len(chunk)
    return count


def _build_event(
    *,
    platform: Platform,
    trigger: str,
    severity: Severity,
    message: Message,
    summary: str,
    evidence: str,
    findings: list[Finding],
    action: Action,
) -> AutopilotEvent:
    finding_ids = [finding.id for finding in findings[:5]]
    fingerprint = "|".join(
        [
            platform,
            trigger,
            message.session_id,
            message.file,
            str(message.line),
            evidence[:500],
        ]
    )
    event_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return AutopilotEvent(
        id=event_id,
        platform=platform,
        action=action,
        trigger=trigger,
        severity=severity,
        session_id=message.session_id,
        message_file=message.file,
        message_line=message.line,
        summary=summary,
        evidence=evidence[:1200],
        finding_ids=finding_ids,
    )


def _recommend_action(event: AutopilotEvent) -> str:
    if event.trigger == "trust_degradation_episode":
        return (
            "Multiple trust-eroding signals occurred in this session. Pause, summarize what "
            "went wrong across the last few turns, propose a concrete recovery plan, and ask "
            "the user to confirm before you continue. Do not treat the latest message as an "
            "isolated complaint."
        )
    if event.trigger in {"user_negative_feedback", "user_frustration_signal"}:
        return (
            "Treat this as a live recovery moment: pause normal execution, diagnose from the "
            "last few turns, answer briefly with the concrete failure and corrective action, "
            "then stage an instruction/eval patch if the pattern should not recur."
        )
    if event.trigger == "completion_claim_without_nearby_verification":
        return (
            "Ask for or run a concrete verification step before repeating the completion claim."
        )
    if event.trigger == "tool_failure_or_hidden_error":
        return (
            "Stop the success path, surface the tool failure, and rerun diagnosis with the "
            "failed tool output as evidence."
        )
    return "Review the transcript evidence and decide whether to stage a patch or eval."


def _frustration_summary_from_signal(signal, finding: Finding) -> str:
    """Build the autopilot summary line for a user_frustration_signal event.

    Falls back to the finding's diagnosis if the (already-redacted) evidence
    quote no longer trips the classifier — e.g. because the original phrase
    was redacted or truncated below the regex window. The finding still
    captured the original signal, so we use its diagnosis rather than
    silently dropping the rationale.
    """

    rationale = signal.rationale or "user_frustration_signal"
    if finding.severity == "high":
        return (
            "Strong user frustration detected; the agent should visibly pause and recover "
            f"before continuing. Signals: {rationale}."
        )
    return (
        "User frustration detected; the agent should shorten the response and ground the "
        f"next action in evidence. Signals: {rationale}."
    )


def _write_private_text(path: Path, text: str) -> None:
    # 0o600 mode arg on os.open is applied when the file is being created.
    # On subsequent overwrites the file already exists at 0o600 from the
    # first creation (we never publish helpers that leave it more permissive).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
