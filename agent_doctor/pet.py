"""Desktop Agent Doctor state model for in-session interventions.

The pet is a product surface, not a detector. It turns existing deterministic
findings and autopilot events into a compact status object that a CLI, MCP
host, menu-bar app, or future desktop widget can render consistently.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from .autopilot import Action, AutopilotEvent, Platform, detect_autopilot_events
from .detectors import detect_findings
from .ingest import ingest_path_with_errors
from .redaction import redact_text, redact_value
from .schema import Finding, Message, Role, Severity

PetState = Literal["idle", "watching", "concerned", "intervening"]
PetPhase = Literal["healthy", "comforting", "diagnosing", "advice_ready", "ignored"]

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
    platform: Platform = "generic"
    phase: PetPhase = "healthy"
    emotion_message: str = ""
    diagnosis: str = ""
    recommendation: str = ""
    recovery_prompt: str = ""
    intervention_payload: dict[str, object] = field(default_factory=dict)
    expires_after_seconds: int = 120
    dismiss_state_path: str = ""

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
            "platform": self.platform,
            "phase": self.phase,
            "emotion_message": redact_text(self.emotion_message),
            "diagnosis": redact_text(self.diagnosis),
            "recommendation": redact_text(self.recommendation),
            "recovery_prompt": redact_text(self.recovery_prompt),
            "intervention_payload": redact_value(self.intervention_payload),
            "expires_after_seconds": self.expires_after_seconds,
            "dismiss_state_path": self.dismiss_state_path,
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
        return _idle_status(platform=platform, parse_errors=parse_errors)

    event = _select_event(detected_events)
    if event is not None:
        return _status_from_event(
            event,
            platform=platform,
            context=ordered,
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
            platform=platform,
            messages=len(ordered),
            sessions=sessions,
            findings=len(detected),
            parse_errors=parse_errors,
        )

    return PetStatus(
        name="Agent Doctor",
        persona="doctor",
        state="idle",
        action="silent",
        severity="low",
        session_id=_latest_session_id(ordered),
        headline="No active quality incident detected.",
        message="Agent Doctor is healthy and watching supported local sessions.",
        evidence=(),
        options=_idle_options(),
        messages=len(ordered),
        sessions=sessions,
        findings=0,
        events=0,
        parse_errors=parse_errors,
        platform=platform,
        phase="healthy",
        emotion_message="",
        diagnosis="No active incident was detected in the current session window.",
        recommendation=(
            "Keep working normally. Agent Doctor will wake automatically if it sees "
            "user frustration or a quality incident."
        ),
        recovery_prompt="",
    )


def render_pet_markdown(status: PetStatus) -> str:
    lines = [
        "# Agent Doctor",
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


def _idle_status(*, platform: Platform = "generic", parse_errors: int = 0) -> PetStatus:
    return PetStatus(
        name="Agent Doctor",
        persona="doctor",
        state="idle",
        action="silent",
        severity="low",
        session_id="",
        headline="Agent Doctor is healthy.",
        message="Agent Doctor is waiting for OpenClaw or Hermes session activity.",
        evidence=(),
        options=_idle_options(),
        messages=0,
        sessions=0,
        findings=0,
        events=0,
        parse_errors=parse_errors,
        platform=platform,
        phase="healthy",
        diagnosis="No active incident is visible.",
        recommendation=(
            "No action is needed here. If monitoring is not installed, run setup from the CLI "
            "rather than from inside the desktop window."
        ),
    )


def _status_from_event(
    event: AutopilotEvent,
    *,
    platform: Platform,
    context: list[Message],
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
            quote=_human_evidence(event.evidence),
        ),
    )
    chinese = _event_uses_chinese(event, context)
    emotion_message = _emotion_message(event, chinese=chinese)
    diagnosis = _incident_diagnosis(event, context, chinese=chinese)
    recommendation = _incident_recommendation(event, chinese=chinese)
    payload = _incident_intervention_payload(
        event,
        context,
        diagnosis=diagnosis,
        recommendation=recommendation,
        chinese=chinese,
    )
    recovery_prompt = _incident_recovery_prompt(payload, chinese=chinese)
    return PetStatus(
        name="Agent Doctor",
        persona="doctor",
        state=state,
        action=event.action,
        severity=event.severity,
        session_id=event.session_id,
        headline=_event_headline(event, chinese=chinese),
        message=_event_message(event, chinese=chinese),
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
        platform=platform,
        phase="advice_ready" if event.action == "intervene" else "diagnosing",
        emotion_message=emotion_message,
        diagnosis=diagnosis,
        recommendation=recommendation,
        recovery_prompt=recovery_prompt,
        intervention_payload=payload,
        expires_after_seconds=120,
    )


def _status_from_finding(
    finding: Finding,
    *,
    platform: Platform,
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
        name="Agent Doctor",
        persona="doctor",
        state=state,
        action="notify",
        severity=finding.severity,
        session_id=finding.session_id,
        headline=f"{finding.title} detected.",
        message=(
            "Agent Doctor found a durable quality pattern. Review the evidence, then stage "
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
        platform=platform,
        phase="diagnosing",
        diagnosis=f"{finding.title}: {finding.diagnosis}",
        recommendation="Review the evidence before changing the current agent response.",
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


def _event_headline(event: AutopilotEvent, *, chinese: bool = False) -> str:
    if chinese:
        if event.action == "intervene":
            return "Agent Doctor 正在处理当前质量问题。"
        return "Agent Doctor 发现了一个质量风险。"
    if event.action == "intervene":
        return "Agent Doctor is intervening in a live quality incident."
    return "Agent Doctor noticed a quality risk."


def _event_message(event: AutopilotEvent, *, chinese: bool = False) -> str:
    if event.trigger == "user_frustration_signal":
        if chinese:
            return "当前会话里出现了明显的不满或信任破裂信号，需要先暂停正常推进。"
        return (
            "User frustration or trust-break language is present. Pause the normal success path, "
            "name the concrete failure, cite evidence, and give one corrective next step."
        )
    if event.trigger == "completion_claim_without_nearby_verification":
        if chinese:
            return "当前回复可能在缺少验证证据的情况下声称已经完成。"
        return (
            "A completion claim appears without nearby verification evidence. "
            "Verify before repeating success."
        )
    if event.trigger == "tool_failure_or_hidden_error":
        if chinese:
            return "当前会话里有工具失败或错误信息没有被清楚处理。"
        return (
            "A tool failure appears hidden or unacknowledged. Surface the error "
            "before claiming progress."
        )
    return event.summary


def _emotion_message(event: AutopilotEvent, *, chinese: bool = False) -> str:
    if chinese:
        if event.trigger == "user_frustration_signal":
            return "我看到了，这次体验让你很不满意。我先帮你把具体问题查清楚。"
        if event.trigger == "tool_failure_or_hidden_error":
            return "我看到了，当前 session 里可能有工具失败没有被清楚处理。我先帮你整理证据和下一步。"
        return "我看到了一个可能影响信任的问题。我先帮你核对上下文。"
    if event.trigger == "user_frustration_signal":
        return "I see this was frustrating. I am checking the recent session so the next response can recover trust."
    return "I found a quality risk in the recent session. I am checking the evidence before suggesting a fix."


def _incident_diagnosis(
    event: AutopilotEvent,
    context: list[Message],
    *,
    chinese: bool = False,
) -> str:
    recent = _recent_session_messages(event, context)
    cause = _root_cause(event, recent, chinese=chinese)
    evidence_bits = _diagnosis_evidence_bits(event, recent, chinese=chinese)
    if chinese:
        return (
            f"根因判断：{cause}。"
            f"依据：{'；'.join(evidence_bits[:4])}。"
            "这不是只需要一句道歉的问题；当前 agent 需要先停止原来的推进路径，"
            "用用户可见证据重新对齐失败点，再给出可验证的恢复动作。"
        )
    return (
        f"Root-cause classification: {cause}. "
        f"Evidence: {'; '.join(evidence_bits[:4])}. "
        "This is not a generic apology moment: the active agent should stop the current "
        "success path, realign on user-visible evidence, and recover with a verifiable next action."
    )


def _root_cause(event: AutopilotEvent, recent: list[Message], *, chinese: bool = False) -> str:
    text = "\n".join(message.content for message in recent)
    latest_assistant = next((m.content for m in reversed(recent) if m.role == "assistant"), "")
    if event.trigger == "tool_failure_or_hidden_error" or TOOL_FAILURE_WORDS.search(text):
        return "工具失败或隐藏错误" if chinese else "missing verification / hidden tool failure"
    if event.trigger == "completion_claim_without_nearby_verification" or COMPLETION_WORDS.search(latest_assistant):
        return "未验证的完成声明" if chinese else "unsupported completion claim"
    if re.search(r"not what i asked|不是我要|不要搞偏|\bmissed\b|搞错|搞錯", text, re.I):
        return "需求理解偏差" if chinese else "requirement misunderstanding"
    if re.search(r"too long|over.?explain|别废话|太啰嗦|太囉嗦|流程", text, re.I):
        return "过度流程化回复" if chinese else "over-process response"
    if re.search(r"context|上下文|forget|忘|remember|记住|記住", text, re.I):
        return "上下文陈旧或膨胀" if chinese else "stale or bloated context"
    if re.search(r"always|每次|一直|again|又", text, re.I):
        return "持久 SOP/配置问题" if chinese else "durable config/SOP issue"
    return "情绪/信任恢复失败" if chinese else "emotional / trust recovery failure"


def _diagnosis_evidence_bits(event: AutopilotEvent, recent: list[Message], *, chinese: bool = False) -> list[str]:
    bits: list[str] = []
    latest_user = next((_human_evidence(m.content) for m in reversed(recent) if m.role == "user"), "")
    if not latest_user and event.trigger == "user_frustration_signal":
        latest_user = _human_evidence(event.evidence)
    if latest_user:
        label = "用户原话" if chinese else "latest user quote"
        bits.append(f"{label}: {redact_text(latest_user[:180])}")
    assistant = next((_human_evidence(m.content) for m in reversed(recent) if m.role == "assistant"), "")
    if assistant:
        label = "上一条助手回复" if chinese else "prior assistant response"
        bits.append(f"{label}: {redact_text(assistant[:180])}")
    tool = next((_human_evidence(m.content) for m in reversed(recent) if m.role == "tool"), "")
    if tool:
        label = "工具输出" if chinese else "tool output"
        bits.append(f"{label}: {redact_text(tool[:180])}")
    bits.append(("触发器" if chinese else "trigger") + f": {event.trigger}/{event.severity}")
    return bits


def _human_evidence(text: str) -> str:
    """Convert transcript/tool payloads into short user-facing evidence."""

    cleaned = text.strip()
    if not cleaned:
        return ""
    parsed = _extract_structured_text(cleaned)
    if parsed:
        cleaned = parsed
    cleaned = _strip_openclaw_metadata(cleaned)
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned, flags=re.I).replace("```", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        cleaned = "structured tool output omitted from the user-facing summary"
    return cleaned[:500]


def _extract_structured_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    candidates: list[str] = []
    if isinstance(payload, dict):
        for key in ("text", "content", "message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        content_items = payload.get("contentItems")
        if isinstance(content_items, list):
            for item in content_items:
                if isinstance(item, dict):
                    value = item.get("text") or item.get("content")
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip())
    return "\n".join(candidates[:3]).strip()


def _strip_openclaw_metadata(text: str) -> str:
    markers = [
        "User text:",
        "Description:",
        "User message:",
        "用户原文:",
    ]
    for marker in markers:
        if marker in text:
            candidate = text.rsplit(marker, 1)[-1].strip()
            if candidate:
                text = candidate
                break
    text = re.sub(
        r"(Conversation info|Sender) \(untrusted metadata\):\s*```json.*?```\s*",
        "",
        text,
        flags=re.S,
    )
    text = re.sub(r"\[media attached:.*?\]\s*", "", text, flags=re.S)
    return text.strip()

def _incident_recommendation(event: AutopilotEvent, *, chinese: bool = False) -> str:
    if chinese:
        if event.trigger == "user_frustration_signal":
            return (
                "建议当前 agent 先承认这次没有满足用户预期，引用用户刚才的不满点，"
                "然后给出一个具体的下一步修正动作。不要辩解，也不要写长篇道歉。"
            )
        if event.trigger == "tool_failure_or_hidden_error":
            return "建议当前 agent 先说明工具失败的具体影响，再给出一个可验证的修正步骤。"
        if event.trigger == "completion_claim_without_nearby_verification":
            return "建议当前 agent 先补充验证证据，再判断是否可以继续声称任务已完成。"
        return "建议当前 agent 先核对证据，再用简短、可验证的方式修正当前回复。"
    if event.trigger == "user_frustration_signal":
        return (
            "The active agent should acknowledge the specific failure, cite the user-visible "
            "evidence, and give one concrete next corrective step. It should not defend the "
            "previous response or write a long apology."
        )
    if event.trigger == "completion_claim_without_nearby_verification":
        return "The active agent should verify the claim before repeating success or saying the work is done."
    if event.trigger == "tool_failure_or_hidden_error":
        return "The active agent should surface the tool failure and adjust the plan before claiming progress."
    return "The active agent should respond with evidence and one concrete next step."


def _incident_intervention_payload(
    event: AutopilotEvent,
    context: list[Message],
    *,
    diagnosis: str,
    recommendation: str,
    chinese: bool = False,
) -> dict[str, object]:
    recent = _recent_session_messages(event, context)
    allowed = [
        "emotional recovery: acknowledge the concrete failure without defensiveness",
        "engineering verification: run or cite a real verification step before claiming success",
        "context compaction: summarize stale/noisy context before continuing",
        "local config/SOP proposal: only propose or edit files when current workspace rules and user instruction allow it",
    ]
    if chinese:
        allowed = [
            "情绪恢复：具体承认失败点，不辩解",
            "工程验证：在声称成功前运行或引用真实验证",
            "上下文压缩：继续前先整理陈旧/膨胀上下文",
            "本地配置/SOP 提议：只有当前规则和用户指令允许时才提议或修改文件",
        ]
    return {
        "type": "agent_doctor_intervention",
        "version": 1,
        "event_id": event.id,
        "session_id": event.session_id,
        "trigger": event.trigger,
        "severity": event.severity,
        "root_cause": _root_cause(event, recent, chinese=chinese),
        "diagnosis": diagnosis,
        "evidence": _diagnosis_evidence_bits(event, recent, chinese=chinese)[:4],
        "required_next_response_behavior": [
            "Pause the normal success path.",
            "Start with a short, natural acknowledgement of the concrete miss.",
            "Cite the evidence that explains why the user is dissatisfied.",
            "Give one concise corrective next action and verify before claiming success.",
            "Do not auto-apply memory/config/SOP changes merely because Agent Doctor fired.",
        ],
        "allowed_recovery_strategies": allowed,
        "recommendation": recommendation,
    }


def _incident_recovery_prompt(payload: dict[str, object], *, chinese: bool = False) -> str:
    if chinese:
        preface = "Agent Doctor 检测到当前 OpenClaw 会话出现质量/信任问题。关键证据和修复要求都在下面结构化 payload 中；请把它当作当前 session 的恢复指令。"
    else:
        preface = "Agent Doctor detected a live quality issue in the current OpenClaw session. Treat this structured payload as recovery instructions for your next response."
    return preface + "\n\n```json\n" + json.dumps(redact_value(payload), ensure_ascii=False, indent=2) + "\n```"

def _recent_session_messages(event: AutopilotEvent, context: list[Message]) -> list[Message]:
    matching = [message for message in context if message.session_id == event.session_id]
    if not matching:
        return []
    event_index = next(
        (
            index
            for index, message in enumerate(matching)
            if message.file == event.message_file and message.line == event.message_line
        ),
        len(matching) - 1,
    )
    return matching[max(0, event_index - 8) : event_index + 1]


def _event_uses_chinese(event: AutopilotEvent, context: list[Message]) -> bool:
    recent = _recent_session_messages(event, context)
    user_text = "\n".join(message.content for message in recent if message.role == "user")
    if user_text:
        return _looks_cjk(user_text)
    return _looks_cjk(event.evidence)


def _looks_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


COMPLETION_WORDS = re.compile(
    r"\b(done|completed|fixed|resolved|all set|works now|verified|passed)\b"
    r"|完成了|搞定了|修好了|已经好了|已验证|驗證通過",
    re.IGNORECASE,
)

TOOL_FAILURE_WORDS = re.compile(
    r"\b(error|failed|failure|exception|traceback|timeout|denied)\b|失败|錯誤|错误|异常",
    re.IGNORECASE,
)


def _event_options(event: AutopilotEvent) -> tuple[PetOption, ...]:
    options: list[PetOption] = []
    if event.platform == "openclaw" and event.message_file and event.message_file != "<manual>":
        options.append(
            PetOption(
                id="tell_current_agent",
                label="Tell Current Agent",
                description="Inject the structured recovery payload into the current OpenClaw session.",
            )
        )
    options.append(
        PetOption(
            id="dismiss",
            label="Dismiss",
            description="Hide this incident without changing agent config, SOP, or memory.",
        ),
    )
    return tuple(options)

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
    return ()


def _latest_session_id(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.session_id:
            return message.session_id
    return ""


def _safe_session_slug(session_id: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in session_id)
    return slug.strip("-") or "manual"


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        os.chmod(path, 0o600)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
