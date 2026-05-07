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
from .frustration import FrustrationSignal, classify_user_frustration
from .ingest import (
    DEFAULT_HERMES_PATH,
    DEFAULT_OPENCLAW_PATH,
    collect_jsonl_paths,
    ingest_path_with_errors,
)
from .redaction import redact_text, redact_value
from .schema import Finding, Message, Severity

Platform = Literal["openclaw", "hermes", "generic"]
Action = Literal["silent", "notify", "intervene"]

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
    pet_state: str = "idle"
    pet_status_path: str | None = None
    pet_card_path: str | None = None

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
            changed_paths = state.changed_jsonl_paths(input_path)
            if first_platform_scan:
                snapshot_paths = changed_paths
                changed_paths = _latest_session_paths(changed_paths)
            messages, parse_errors = _ingest_paths(changed_paths)
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
    suppressed = 0
    try:
        for event in candidates:
            if state.should_emit(event, cooldown_seconds=cooldown_seconds):
                event = write_diagnosis_card(out_dir, event, findings)
                append_event(out_dir / "events.jsonl", event)
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
                suppressed += 1
    finally:
        state.close()

    pet_status_path: str | None = None
    pet_card_path: str | None = None
    pet_state = "idle"
    try:
        from .pet import build_pet_status, write_pet_artifacts

        event_overrides = {event.id: event for event in emitted}
        pet_events = [event_overrides.get(event.id, event) for event in candidates]
        pet_status = build_pet_status(
            messages,
            findings,
            platform=platform,
            events=pet_events,
            parse_errors=parse_errors,
        )
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


def _ingest_paths(paths: list[Path]) -> tuple[list[Message], int]:
    messages: list[Message] = []
    parse_errors = 0
    for path in paths:
        file_messages, file_errors = ingest_path_with_errors(path)
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
    ordered = list(messages)
    findings = list(findings)
    findings_by_session: dict[str, list[Finding]] = {}
    for finding in findings:
        findings_by_session.setdefault(finding.session_id, []).append(finding)

    events: list[AutopilotEvent] = []
    for index, message in enumerate(ordered):
        session_findings = findings_by_session.get(message.session_id, [])
        if message.role == "user":
            frustration = _classify_user_message(message, ordered, index, platform)
        else:
            frustration = FrustrationSignal(matched=False)
        if frustration.matched and frustration.severity in {"medium", "high"}:
            events.append(
                _build_event(
                    platform=platform,
                    trigger="user_frustration_signal",
                    severity=frustration.severity,
                    message=message,
                    summary=_frustration_summary(frustration),
                    evidence=message.content,
                    findings=session_findings,
                    action="intervene" if frustration.severity == "high" else "notify",
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
        lines.extend(
            [
                "",
                "## Immediate Agent Instruction",
                "",
                "Pause the normal success path. Answer the user with a concise recovery response: "
                "name the concrete failure, cite the evidence you used, and state the next corrective action. "
                "Do not defend the prior response or write a long apology.",
            ]
        )
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
        f"- Action: `{event.action}`",
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


def _classify_user_message(
    message: Message,
    ordered: list[Message],
    index: int,
    platform: Platform,
) -> FrustrationSignal:
    """Phase 2 fused classification with recent-message context.

    Falls back to Tier 1 only if the classifier package isn't importable
    (defensive) or no adapter is detected for the platform.
    """
    try:
        from .classifier.fused import fused_classify
        from .classifier.user_dict import load_user_dict
    except ImportError:
        return classify_user_frustration(message.content)

    # Build recent user messages for the same session
    recent: list[str] = []
    for prior in ordered[: index + 1]:
        if prior.role == "user" and prior.session_id == message.session_id:
            recent.append(prior.content)
    recent = recent[-10:]  # last 10 user messages

    # User dict
    user_dict = None
    try:
        dict_path = Path("~/.agent-doctor").expanduser() / platform / "user-dict.json"
        user_dict = load_user_dict(dict_path)
    except Exception:
        pass

    # Live monitoring must stay fast and local. Host-inference Tier 2 remains
    # outside the production sidecar path.
    try:
        return fused_classify(
            message.content,
            recent_user_messages=recent,
            user_dict=user_dict,
        )
    except Exception:
        # Defensive: if fused fails for any reason, fall back to Tier 1
        return classify_user_frustration(message.content)


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


def _frustration_summary(signal: FrustrationSignal) -> str:
    if signal.severity == "high":
        return (
            "Strong user frustration detected; the agent should visibly pause and recover "
            f"before continuing. Signals: {signal.rationale}."
        )
    return (
        "User frustration detected; the agent should shorten the response and ground the "
        f"next action in evidence. Signals: {signal.rationale}."
    )


def _write_private_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.chmod(path, 0o600)
