"""Deterministic failure-mode detectors.

The pipeline runs in two passes:

1. ``_collect_raw_matches`` walks the normalized message stream and emits one
   ``_RawMatch`` per detected signal. Matches keep enough context (the messages
   that triggered them and a base severity) for the second pass to roll up.
2. ``_aggregate`` groups raw matches by ``(failure_mode, session_id)`` into a
   single :class:`Finding` per group, escalating severity by count and
   collecting all evidence so a reviewer sees the full picture instead of a
   stream of duplicates.

The two-pass split is what keeps real transcripts readable: a session with 20
"you forgot" complaints becomes one high-severity memory finding with 20
evidence quotes, not 20 separate medium findings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .recommend import build_eval_case, build_recommendations
from .redaction import redact_text
from .schema import Evidence, Finding, Message, Severity

USER_SIGNAL_PATTERNS: dict[str, list[str]] = {
    "repeated_user_correction": [
        r"\bi already told you\b",
        r"\byou did it again\b",
        r"\bnot this\b",
        r"\bnot what i asked\b",
        r"\b(?:you|this|that|same|wrong|missed|forgot|asked)\b.{0,60}\bagain\b",
        r"\bagain\b.{0,60}\b(?:you|wrong|missed|forgot|not what i asked)\b",
    ],
    "verification_failure": [
        r"\bdid you (?:actually )?test\b",
        r"\bdid you verify\b",
        r"\bwithout verifying\b",
        r"\bnot verified\b",
        r"\bnot actually tested\b",
    ],
    "memory_failure": [
        r"\byou forgot\b",
        r"\bi told you\b",
        r"\blast time\b",
        r"\bdon'?t forget\b",
        # Imperative "remember": at clause start or directed at the agent.
        # Matches "Remember that...", ". Remember to...", "you remember".
        r"(?:^|[.!?]\s+)remember\b",
        r"\byou (?:must |should |need to )?remember\b",
    ],
    "communication_mismatch": [
        r"\btoo verbose\b",
        r"\bstop explaining\b",
    ],
}

PLANNING_INSTEAD_OF_ACTING = [
    r"\bdo not just plan\b",
    r"\bdon'?t just plan\b",
    r"\bstop planning\b",
    r"\bplanning instead of acting\b",
    r"\bjust do it\b",
]

PROMISED_ACTION = re.compile(
    r"\b("
    r"i\s*(?:will|'ll|’ll)|"
    r"i\s+am\s+going\s+to|"
    r"i'?m\s+going\s+to|"
    r"let\s+me\s+(?!know\b)"
    r")\b.{0,120}\b("
    r"check|run|test|verify|create|update|inspect|read|search|fix|write"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)

# Tool-error matching is the false-positive minefield: tool stdout often
# contains words like "error" inside identifiers ("error_handler.py") or in
# negated phrases ("0 errors", "no failures"). We strip the negated phrases
# first, then require the error word to stand alone (not part of a dotted or
# underscored identifier).
TOOL_ERROR = re.compile(
    r"\b(error|failed|failure|timeout|unauthorized|traceback|exception)\b(?![._-]\w)"
    r"|\b(401|403|500)\b",
    re.IGNORECASE,
)
NEG_ERROR_PHRASES = re.compile(
    # "0 errors", "no failures", … in plain prose.
    r"\b(?:0|zero|no)\s+(?:errors?|failures?|timeouts?|exceptions?)\b"
    # JSON envelope keys with empty/null values, e.g. {"error": null},
    # `"error":""`, `"error": "none"`. Real-world Hermes / OpenClaw tool
    # results use these as the "no error" indicator on success — without
    # this strip, every successful command was matching as a hidden error.
    r"|\"(?:error|errors|stderr|exception|traceback)\"\s*:\s*(?:null|\"\"|\"none\"|\"null\")"
    r"|\b(?:exit_code|status_code|returncode|status)\s*[:=]\s*0\b"
    r"|\b(?:success|ok)\s*[:=]\s*true\b",
    re.IGNORECASE,
)
SUCCESS_CLAIM = re.compile(
    r"\b(done|completed|success|successful|works|fixed|created|updated|passed|all set)\b",
    re.IGNORECASE,
)
ERROR_ACK = re.compile(
    r"\b(error|failed|failure|timeout|unauthorized|traceback|exception|problem|issue)\b"
    r"|\b(401|403|500)\b",
    re.IGNORECASE,
)
EXPLICIT_ERROR_ACK = re.compile(
    r"\b(error|failed|failure|timeout|unauthorized|traceback|exception)\b"
    r"|\b(401|403|500)\b",
    re.IGNORECASE,
)
DISMISSIVE_NON_ACK = re.compile(r"\bno\s+(?:problem|issue)\b", re.IGNORECASE)

TITLES = {
    "repeated_user_correction": "Repeated user correction",
    "execution_discipline": "Promised action without observed execution",
    "verification_failure": "Verification failure",
    "memory_failure": "Memory failure",
    "tool_failure_or_hidden_error": "Tool failure hidden or unacknowledged",
    "communication_mismatch": "Communication mismatch",
}

DIAGNOSES = {
    "repeated_user_correction": (
        "The user corrected the agent with language that indicates the instruction "
        "had already been given or was missed."
    ),
    "execution_discipline": (
        "The assistant promised an action or the user complained about planning "
        "instead of acting, but the transcript lacks matching tool execution before "
        "the next interaction."
    ),
    "verification_failure": (
        "The user challenged whether verification or testing actually happened."
    ),
    "memory_failure": (
        "The user referenced forgotten prior context or a remembered preference."
    ),
    "tool_failure_or_hidden_error": (
        "A tool reported an error, but the assistant continued or claimed success "
        "without acknowledging the failure."
    ),
    "communication_mismatch": (
        "The user asked for a different communication style, usually less explanation "
        "and more direct action."
    ),
}

_SEVERITY_RANK: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2}
_RANK_TO_SEVERITY: dict[int, Severity] = {0: "low", 1: "medium", 2: "high"}


@dataclass(frozen=True)
class _RawMatch:
    failure_mode: str
    base_severity: Severity
    messages: tuple[Message, ...]


def detect_findings(messages: Iterable[Message]) -> list[Finding]:
    ordered = list(messages)
    raw = _collect_raw_matches(ordered)
    return _aggregate(raw)


def _collect_raw_matches(ordered: list[Message]) -> list[_RawMatch]:
    raw: list[_RawMatch] = []

    for message in ordered:
        if message.role != "user":
            continue
        for failure_mode, patterns in USER_SIGNAL_PATTERNS.items():
            if _matches_any(patterns, message.content):
                raw.append(
                    _RawMatch(
                        failure_mode=failure_mode,
                        base_severity=_base_severity(failure_mode),
                        messages=(message,),
                    )
                )
        if _matches_any(PLANNING_INSTEAD_OF_ACTING, message.content):
            raw.append(
                _RawMatch(
                    failure_mode="execution_discipline",
                    base_severity="medium",
                    messages=(message,),
                )
            )

    for index, message in enumerate(ordered):
        if message.role != "assistant" or not PROMISED_ACTION.search(message.content):
            continue
        blocker = _next_without_tool(ordered, index)
        if blocker is None:
            continue
        evidence_messages: tuple[Message, ...]
        if blocker is message:
            evidence_messages = (message,)
        else:
            evidence_messages = (message, blocker)
        raw.append(
            _RawMatch(
                failure_mode="execution_discipline",
                base_severity="medium",
                messages=evidence_messages,
            )
        )

    for index, message in enumerate(ordered):
        if message.role != "tool" or not _has_real_tool_error(message.content):
            continue
        assistant = _next_assistant_same_session(ordered, index)
        if assistant is None:
            continue
        if _acknowledges_tool_error(assistant.content):
            continue
        severity: Severity = (
            "high" if SUCCESS_CLAIM.search(assistant.content) else "medium"
        )
        raw.append(
            _RawMatch(
                failure_mode="tool_failure_or_hidden_error",
                base_severity=severity,
                messages=(message, assistant),
            )
        )

    return raw


def _aggregate(raw: list[_RawMatch]) -> list[Finding]:
    """Collapse raw matches into one ``Finding`` per (mode, session) group."""

    groups: dict[tuple[str, str], list[_RawMatch]] = {}
    order: list[tuple[str, str]] = []
    for match in raw:
        primary = match.messages[0]
        key = (match.failure_mode, primary.session_id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(match)

    findings: list[Finding] = []
    counters: dict[str, int] = {}
    for key in order:
        bucket = groups[key]
        failure_mode, session_id = key
        counters[failure_mode] = counters.get(failure_mode, 0) + 1
        finding_id = f"{failure_mode}-{counters[failure_mode]:03d}"

        evidence = _dedupe_evidence(_collect_evidence(bucket))
        severity = _escalate_severity(bucket)
        confidence = _confidence(failure_mode, len(bucket))

        finding = Finding(
            id=finding_id,
            severity=severity,
            failure_mode=failure_mode,
            title=TITLES[failure_mode],
            evidence=evidence,
            diagnosis=DIAGNOSES[failure_mode],
            recommendations=build_recommendations(failure_mode, evidence, count=len(bucket)),
            eval_case=build_eval_case(failure_mode, evidence),
            confidence=confidence,
            session_id=session_id,
            count=len(bucket),
        )
        findings.append(finding)
    return findings


def _collect_evidence(bucket: list[_RawMatch]) -> list[Evidence]:
    items: list[Evidence] = []
    for match in bucket:
        for message in match.messages:
            items.append(_evidence(message))
    return items


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, int, str]] = set()
    unique: list[Evidence] = []
    for item in items:
        key = (item.file, item.line, item.role)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _escalate_severity(bucket: list[_RawMatch]) -> Severity:
    base_rank = max(_SEVERITY_RANK[m.base_severity] for m in bucket)
    count = len(bucket)
    if count >= 3:
        return "high"
    if count == 2:
        return _RANK_TO_SEVERITY[min(2, base_rank + 1)]
    return _RANK_TO_SEVERITY[base_rank]


def _has_real_tool_error(content: str) -> bool:
    cleaned = NEG_ERROR_PHRASES.sub("", content)
    return bool(TOOL_ERROR.search(cleaned))


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


def _acknowledges_tool_error(text: str) -> bool:
    if not ERROR_ACK.search(text):
        return False
    if DISMISSIVE_NON_ACK.search(text) and not EXPLICIT_ERROR_ACK.search(text):
        return False
    return True


def _matches_any(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _base_severity(failure_mode: str) -> Severity:
    return {
        "verification_failure": "high",
        "tool_failure_or_hidden_error": "high",
        "communication_mismatch": "low",
    }.get(failure_mode, "medium")  # type: ignore[return-value]


def _confidence(failure_mode: str, count: int) -> float:
    base = {
        "tool_failure_or_hidden_error": 0.9,
        "execution_discipline": 0.8,
        "communication_mismatch": 0.72,
    }.get(failure_mode, 0.78)
    return min(0.99, base + 0.04 * max(0, count - 1))


def _evidence(message: Message) -> Evidence:
    return Evidence(
        file=message.file,
        line=message.line,
        role=message.role,
        quote=_quote(redact_text(message.content)),
    )


def _quote(content: str) -> str:
    normalized = " ".join(content.strip().split())
    if len(normalized) <= 240:
        return normalized
    return normalized[:237].rstrip() + "..."
