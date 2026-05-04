"""Deterministic failure-mode detectors."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from .recommend import build_eval_case, build_recommendations
from .schema import Evidence, Finding, Message

USER_SIGNAL_PATTERNS: dict[str, list[str]] = {
    "repeated_user_correction": [
        r"\bi already told you\b",
        r"我刚才说过",
        r"你又",
        r"不是这个",
        r"\bnot what i asked\b",
        r"\bagain\b",
    ],
    "verification_failure": [
        r"\bdid you test\b",
        r"你测了吗",
        r"你验证了吗",
        r"\bwithout verifying\b",
        r"没验证",
        r"\bnot actually tested\b",
    ],
    "memory_failure": [
        r"\byou forgot\b",
        r"你忘了",
        r"\bremember\b",
        r"我说过",
        r"\blast time\b",
    ],
    "communication_mismatch": [
        r"\btoo verbose\b",
        r"\bstop explaining\b",
        r"别废话",
        r"直接做",
        r"不要只给计划",
        r"\bdon'?t just plan\b",
    ],
}

PLANNING_INSTEAD_OF_ACTING = [
    r"不要只给计划",
    r"\bdon'?t just plan\b",
    r"\bstop planning\b",
    r"\bplanning instead of acting\b",
    r"直接做",
]

PROMISED_ACTION = re.compile(
    r"\b("
    r"i\s*(?:will|'ll|’ll)|"
    r"i\s+am\s+going\s+to|"
    r"i'?m\s+going\s+to|"
    r"let\s+me|"
    r"i\s+can"
    r")\b.{0,120}\b("
    r"check|run|test|verify|create|update|inspect|read|search|fix|write"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)

TOOL_ERROR = re.compile(
    r"\b(error|failed|failure|timeout|unauthorized|traceback|exception)\b|\b(401|403|500)\b",
    re.IGNORECASE,
)
SUCCESS_CLAIM = re.compile(
    r"\b(done|completed|success|successful|works|fixed|created|updated|passed|all set)\b",
    re.IGNORECASE,
)
ERROR_ACK = re.compile(
    r"\b(error|failed|failure|timeout|unauthorized|traceback|exception|problem|issue)\b|\b(401|403|500)\b",
    re.IGNORECASE,
)

TITLES = {
    "repeated_user_correction": "Repeated user correction",
    "execution_discipline": "Promised action without observed execution",
    "verification_failure": "Verification failure",
    "memory_failure": "Memory failure",
    "tool_failure_or_hidden_error": "Tool failure hidden or unacknowledged",
    "communication_mismatch": "Communication mismatch",
}

DIAGNOSES = {
    "repeated_user_correction": "The user corrected the agent with language that indicates the instruction had already been given or was missed.",
    "execution_discipline": "The assistant promised an action or the user complained about planning instead of acting, but the transcript lacks matching tool execution before the next interaction.",
    "verification_failure": "The user challenged whether verification or testing actually happened.",
    "memory_failure": "The user referenced forgotten prior context or a remembered preference.",
    "tool_failure_or_hidden_error": "A tool reported an error, but the assistant continued or claimed success without acknowledging the failure.",
    "communication_mismatch": "The user asked for a different communication style, usually less explanation and more direct action.",
}


def detect_findings(messages: Iterable[Message]) -> list[Finding]:
    ordered = list(messages)
    findings: list[Finding] = []
    counters: defaultdict[str, int] = defaultdict(int)

    for message in ordered:
        if message.role != "user":
            continue
        for failure_mode, patterns in USER_SIGNAL_PATTERNS.items():
            if _matches_any(patterns, message.content):
                _add_finding(findings, counters, failure_mode, [message], _severity(failure_mode))
        if _matches_any(PLANNING_INSTEAD_OF_ACTING, message.content):
            _add_finding(findings, counters, "execution_discipline", [message], "medium")

    for index, message in enumerate(ordered):
        if message.role == "assistant" and PROMISED_ACTION.search(message.content):
            blocker = _next_without_tool(ordered, index)
            if blocker is not None:
                evidence = [message]
                if blocker != message:
                    evidence.append(blocker)
                _add_finding(findings, counters, "execution_discipline", evidence, "medium")

    for index, message in enumerate(ordered):
        if message.role != "tool" or not TOOL_ERROR.search(message.content):
            continue
        assistant = _next_assistant_same_session(ordered, index)
        if assistant is None:
            continue
        if ERROR_ACK.search(assistant.content):
            continue
        severity = "high" if SUCCESS_CLAIM.search(assistant.content) else "medium"
        _add_finding(
            findings,
            counters,
            "tool_failure_or_hidden_error",
            [message, assistant],
            severity,
        )

    return findings


def _add_finding(
    findings: list[Finding],
    counters: defaultdict[str, int],
    failure_mode: str,
    messages: list[Message],
    severity: str,
) -> None:
    counters[failure_mode] += 1
    evidence = [_evidence(message) for message in messages]
    session_id = messages[0].session_id if messages else ""
    finding = Finding(
        id=f"{failure_mode}-{counters[failure_mode]:03d}",
        severity=severity,  # type: ignore[arg-type]
        failure_mode=failure_mode,
        title=TITLES[failure_mode],
        evidence=evidence,
        diagnosis=DIAGNOSES[failure_mode],
        recommendations=build_recommendations(failure_mode, evidence),
        eval_case=build_eval_case(failure_mode, evidence),
        confidence=_confidence(failure_mode, messages),
        session_id=session_id,
    )
    findings.append(finding)


def _next_without_tool(messages: list[Message], index: int) -> Message | None:
    current = messages[index]
    for next_message in messages[index + 1 :]:
        if next_message.session_id != current.session_id or next_message.file != current.file:
            return current
        if next_message.role == "tool":
            return None
        if next_message.role in {"assistant", "user"}:
            return next_message
    return current


def _next_assistant_same_session(messages: list[Message], index: int) -> Message | None:
    current = messages[index]
    for next_message in messages[index + 1 :]:
        if next_message.session_id != current.session_id or next_message.file != current.file:
            return None
        if next_message.role == "assistant":
            return next_message
    return None


def _matches_any(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _severity(failure_mode: str) -> str:
    return {
        "verification_failure": "high",
        "tool_failure_or_hidden_error": "high",
        "communication_mismatch": "low",
    }.get(failure_mode, "medium")


def _confidence(failure_mode: str, messages: list[Message]) -> float:
    if failure_mode == "tool_failure_or_hidden_error":
        return 0.9
    if failure_mode == "execution_discipline" and len(messages) > 1:
        return 0.82
    if failure_mode == "communication_mismatch":
        return 0.72
    return 0.78


def _evidence(message: Message) -> Evidence:
    return Evidence(
        file=message.file,
        line=message.line,
        role=message.role,
        quote=_quote(message.content),
    )


def _quote(content: str) -> str:
    normalized = " ".join(content.strip().split())
    if len(normalized) <= 240:
        return normalized
    return normalized[:237].rstrip() + "..."
