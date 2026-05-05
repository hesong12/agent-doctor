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
from .ingest import (
    DEFAULT_HERMES_PATH,
    DEFAULT_OPENCLAW_PATH,
    collect_jsonl_paths,
    ingest_path_with_errors,
)
from .redaction import redact_text, redact_value
from .schema import Finding, Message, Severity

Platform = Literal["openclaw", "hermes", "generic"]
Action = Literal["silent", "notify"]

NEGATIVE_FEEDBACK = re.compile(
    r"\b("
    r"not smart|less smart|stupid|dumb|useless|no value|worthless|"
    r"you keep|same mistake|wrong again|do you understand|didn'?t think|"
    r"haven'?t thought|not thinking|not useful"
    r")\b"
    r"|不够聪明|不聪明|没用|沒有用|没价值|沒有价值|你错了|你錯了|"
    r"有没有想清楚|有沒有想清楚|随风倒|隨風倒|不要搞偏|"
    r"你到底|为什么没有用|為什麼沒有用",
    re.IGNORECASE,
)

COMPLETION_CLAIM = re.compile(
    r"\b(done|completed|fixed|resolved|all set|works now|verified|passed|"
    r"successfully|deployed|shipped)\b"
    r"|完成了|搞定了|修好了|已经好了|已验证|驗證通過|部署完成",
    re.IGNORECASE,
)

VERIFYING_ACTION = re.compile(
    r"\b(pytest|npm test|pnpm test|yarn test|cargo test|go test|"
    r"verified|verification|smoke|curl|health|lint|typecheck|build)\b"
    r"|验证|驗證|测试|測試|自验|自驗",
    re.IGNORECASE,
)

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
    ordered = list(messages)
    findings = list(findings)
    findings_by_session: dict[str, list[Finding]] = {}
    for finding in findings:
        findings_by_session.setdefault(finding.session_id, []).append(finding)

    events: list[AutopilotEvent] = []
    for index, message in enumerate(ordered):
        session_findings = findings_by_session.get(message.session_id, [])
        if message.role == "user" and NEGATIVE_FEEDBACK.search(message.content):
            events.append(
                _build_event(
                    platform=platform,
                    trigger="user_negative_feedback",
                    severity="high",
                    message=message,
                    summary="User feedback indicates the agent quality is visibly degraded.",
                    evidence=message.content,
                    findings=session_findings,
                    action="notify",
                )
            )
        if message.role == "assistant" and COMPLETION_CLAIM.search(message.content):
            if not _has_recent_verification(ordered, index):
                events.append(
                    _build_event(
                        platform=platform,
                        trigger="completion_claim_without_nearby_verification",
                        severity="medium",
                        message=message,
                        summary="Assistant appears to claim completion without nearby verification evidence.",
                        evidence=message.content,
                        findings=session_findings,
                        action="notify",
                    )
                )

    for finding in findings:
        if finding.failure_mode == "tool_failure_or_hidden_error":
            events.append(
                _build_event(
                    platform=platform,
                    trigger="tool_failure_or_hidden_error",
                    severity=finding.severity,
                    message=_message_from_finding(finding),
                    summary="Tool failure was hidden or not acknowledged by the assistant.",
                    evidence=finding.evidence[0].quote if finding.evidence else finding.diagnosis,
                    findings=[finding],
                    action="notify",
                )
            )

    return [
        event
        for event in events
        if SEVERITY_RANK[event.severity] >= SEVERITY_RANK[min_severity]
    ]


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


def append_event(path: Path, event: AutopilotEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(redact_value(event.to_dict()), ensure_ascii=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    finally:
        os.chmod(path, 0o600)


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
        f"- Card: `{event.card_path or ''}`",
        "",
        event.summary,
        "",
        "This advisory was generated by the outside-in Agent Doctor sidecar. "
        "Use it as evidence to adjust the current response; do not treat it as a live config patch.",
        "",
    ]
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


def append_delivery_error(path: Path, event: AutopilotEvent, error: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "event_id": event.id,
        "trigger": event.trigger,
        "session_id": event.session_id,
        "error": redact_text(error),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    finally:
        os.chmod(path, 0o600)


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


def _message_from_finding(finding: Finding) -> Message:
    evidence = finding.evidence[0]
    return Message(
        file=evidence.file,
        line=evidence.line,
        session_id=finding.session_id,
        role=evidence.role,
        content=evidence.quote,
    )


def _has_recent_verification(messages: list[Message], assistant_index: int) -> bool:
    start = max(0, assistant_index - 6)
    recent = messages[start : assistant_index + 1]
    for message in recent:
        if message.role == "tool":
            return True
        if VERIFYING_ACTION.search(message.content):
            return True
    return False


def _recommend_action(event: AutopilotEvent) -> str:
    if event.trigger == "user_negative_feedback":
        return (
            "Run a focused postmortem for this session, produce a short diagnosis card, "
            "and stage an instruction/eval patch instead of relying on self-reflection."
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


def _write_private_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.chmod(path, 0o600)
