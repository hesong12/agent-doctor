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

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["events"] = [event.to_dict() for event in self.events]
        return data


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
    changed_only: bool = False,
) -> AutopilotResult:
    input_path = path or default_transcript_path(platform)
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = AutopilotState(state_path or out_dir / "state.sqlite3")
    changed_paths: list[Path] | None = None
    try:
        if changed_only:
            changed_paths = state.changed_jsonl_paths(input_path)
            messages, parse_errors = _ingest_paths(changed_paths)
        else:
            messages, parse_errors = ingest_path_with_errors(input_path)
            changed_paths = collect_jsonl_paths(input_path)
        state.record_file_snapshots(changed_paths)
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
    suppressed = 0
    try:
        for event in candidates:
            if state.should_emit(event, cooldown_seconds=cooldown_seconds):
                event = write_diagnosis_card(out_dir, event, findings)
                append_event(out_dir / "events.jsonl", event)
                write_regression_eval(out_dir / "regressions", event)
                if inbox_dir is not None:
                    write_inbox_advisory(inbox_dir, event)
                if notify_command:
                    error = run_notify_command(notify_command, event)
                    if error:
                        delivery_errors.append(error)
                        append_delivery_error(out_dir / "delivery-errors.jsonl", event, error)
                state.record(event)
                emitted.append(event)
            else:
                suppressed += 1
    finally:
        state.close()

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


def _ingest_paths(paths: list[Path]) -> tuple[list[Message], int]:
    messages: list[Message] = []
    parse_errors = 0
    for path in paths:
        file_messages, file_errors = ingest_path_with_errors(path)
        messages.extend(file_messages)
        parse_errors += file_errors
    return messages, parse_errors


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
        f"> {redact_text(event.evidence).strip()}",
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
        subprocess.run(args, check=True, text=True, capture_output=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return None


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

    def record(self, event: AutopilotEvent) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO emitted_events(id, session_id, trigger, emitted_at)
            VALUES (?, ?, ?, ?)
            """,
            (event.id, event.session_id, event.trigger, time.time()),
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
