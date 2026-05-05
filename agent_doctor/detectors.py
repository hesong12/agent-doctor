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

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .frustration import classify_user_frustration
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
        r"我(?:已经|已經)(?:说过|說過|讲过|講過|告诉过你|告訴過你)",
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
    "missed_core_question": [
        r"\byou (?:didn'?t|did not) answer (?:my|the) (?:question|point)\b",
        r"\banswer my (?:actual )?question\b",
        r"\bthat'?s not (?:what|the question) i asked\b",
        r"\bi asked you (?:about|to)\b.{0,80}\bnot\b",
        r"你没回答(?:我的)?问题|你沒回答(?:我的)?問題",
        r"我问的是|我問的是|答非所问|答非所問",
    ],
    "instruction_drift": [
        r"\bi (?:didn'?t|did not) ask (?:you )?(?:to|for)\b",
        r"\bnobody asked (?:you )?(?:to|for)\b",
        r"\bwhy are you (?:also )?(?:doing|adding|changing|fixing|writing)\b",
        r"\bjust (?:do|fix|answer) what i asked\b",
        r"\bstop adding extra\b",
        r"\bdon'?t go beyond (?:what|the scope)\b",
        r"我没让你|我沒讓你|没让你|沒讓你",
        r"我只让你|我只讓你",
        r"不要超出",
    ],
    "over_process_response": [
        r"\bstop (?:narrating|describing what you'?re doing)\b",
        r"\bjust (?:give|show) (?:me )?(?:the )?(?:answer|result|output)\b",
        r"\btoo (?:much )?(?:meta|process|narration)\b",
        r"\bi don'?t (?:need|want) (?:the )?(?:play[- ]by[- ]play|process|narration)\b",
        r"别(?:再)?(?:讲|說|说)?(?:你|自己)?(?:在做什么|在幹什麼|的过程|的過程)",
        r"少(?:废话|廢話|说点|說點)",
    ],
}

# Phrases that indicate the user is challenging an assistant completion claim
# (used together with COMPLETION_CLAIM_PATTERN to detect unsupported claims).
USER_DOUBT_OF_COMPLETION = [
    r"\bare you sure\b",
    r"\bdoesn'?t (?:look|seem) (?:done|fixed|finished|complete)\b",
    r"\bit'?s not (?:done|fixed|finished|working|complete)\b",
    r"\bdid you actually (?:do|finish|run|test) it\b",
    r"你确定(?:做完|搞定|修好)了|你確定(?:做完|搞定|修好)了",
    r"没做完|沒做完|没修好|沒修好|没改完|沒改完",
]

# Assistant phrases used to detect unsupported_completion_claim and
# over_process_response on the assistant side.
COMPLETION_CLAIM_PATTERN = re.compile(
    r"\b(?:done|completed|fixed|resolved|all set|works now|verified|passed|"
    r"successfully|deployed|shipped|finished|ready)\b"
    r"|完成了|搞定了|修好了|已经好了|已驗證|验证通过|部署完成",
    re.IGNORECASE,
)

# Process-narration pattern for detecting over_process_response on the
# assistant side. Many short matches in a single message indicate excessive
# meta-narration ("first I'll … then I'll …").
PROCESS_NARRATION_TOKEN = re.compile(
    r"\b(?:i'?m going to|i will|let me|i'?ll|first(?:,|\s)|then(?:,|\s)|next(?:,|\s)|"
    r"after that|now i'?ll|finally(?:,|\s))\b",
    re.IGNORECASE,
)

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
    # HTTP status codes — but not when they're inside a `file:line:col`
    # source-code reference (matched `403` in `cli.js:403:` on a real
    # Hermes session). Require word-boundary on both sides AND not
    # preceded/followed by a colon (which is the common file:line:col
    # source ref shape).
    r"|(?<![:.\d])(401|403|500)(?![:\d])\b",
    re.IGNORECASE,
)
NEG_ERROR_PHRASES = re.compile(
    # "0 errors", "no failures", … in plain prose.
    r"\b(?:0|zero|no)\s+(?:errors?|failures?|timeouts?|exceptions?)\b"
    # JSON envelope keys with empty/null values, e.g. `{"error": null}`,
    # `"error":""`, `"error": "none"`. Real Hermes / OpenClaw tool results
    # use these as the "no error" indicator on success — without this
    # strip, every successful command matches as a hidden error.
    r"|\"(?:error|errors|stderr|exception|traceback)\"\s*:\s*(?:null|\"\"|\"none\"|\"null\")"
    # `"exit_code": 0`, `"status": 0`, `exit_code: 0`, `status=0` …
    # Optional surrounding quotes handle both JSON and bare-prose forms.
    r"|\"?(?:exit_code|status_code|returncode|status)\"?\s*[:=]\s*0\b"
    # `"success": true`, `success: true`, `"ok": true`, …
    r"|\"?(?:success|ok)\"?\s*[:=]\s*true\b"
    # `exit_code_meaning` fields explicitly disclaim non-zero exits.
    r"|\(?\s*not an error\s*\)?",
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
    "user_frustration_signal": "User frustration or trust-break signal",
    "trust_degradation_episode": "Trust degradation episode",
    "missed_core_question": "Missed the user's core question",
    "instruction_drift": "Instruction drift / scope inflation",
    "over_process_response": "Over-process / meta-narration response",
    "unsupported_completion_claim": "Unsupported completion claim",
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
    "user_frustration_signal": (
        "The user showed strong frustration, direct insult/profanity, repeated correction, "
        "or trust-break language that should trigger an immediate recovery response."
    ),
    "trust_degradation_episode": (
        "Multiple frustration / correction signals occurred close together in the same "
        "session, indicating a cumulative trust-loss episode rather than an isolated "
        "complaint. The agent should pause, acknowledge the pattern, and ground the next "
        "response in concrete recovery steps."
    ),
    "missed_core_question": (
        "The user said the agent did not answer the actual question (or answered a "
        "different one). The agent should re-anchor on the original question before "
        "continuing."
    ),
    "instruction_drift": (
        "The user pointed out that the agent did or added something it was not asked "
        "to do, indicating scope inflation or instruction drift away from the original "
        "request."
    ),
    "over_process_response": (
        "The user complained about excessive process narration or play-by-play, asking "
        "for the result rather than the meta description of how the agent will get there."
    ),
    "unsupported_completion_claim": (
        "The assistant claimed completion (done / fixed / verified / passed) without "
        "an observable verification step, or the user immediately challenged the claim."
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
    raw.extend(_detect_trust_degradation_episodes(ordered, raw))
    return _aggregate(raw)


# Maximum number of user turns that may separate two trust-eroding signals
# before they're treated as separate incidents instead of one episode.
TRUST_EPISODE_USER_TURN_WINDOW = 6

# Failure modes that, when seen close together in a single session, indicate
# a cumulative trust-degradation episode instead of an isolated complaint.
TRUST_EPISODE_TRIGGER_MODES = frozenset(
    {
        "user_frustration_signal",
        "repeated_user_correction",
        "verification_failure",
        "memory_failure",
        "missed_core_question",
        "instruction_drift",
        "over_process_response",
        "communication_mismatch",
    }
)


def _detect_trust_degradation_episodes(
    ordered: list[Message],
    raw: list[_RawMatch],
) -> list[_RawMatch]:
    """Aggregate multiple recent trust-eroding signals into episode matches.

    Episode logic stays outside the per-mode aggregator because the episode is
    *cross-mode*: e.g. a memory miss followed two turns later by a frustration
    signal is one episode, not two unrelated findings. The two-pass aggregator
    only collapses raw matches of the same mode.

    User-turn indexes are computed *per session* (not globally across the
    whole transcript stream). Two same-session triggers must remain close in
    same-session user-turn space; user turns from other interleaved sessions
    must not dilate that gap. This is what keeps the window meaningful when
    multiple sessions are scanned in a single pass.
    """

    # Per-session user-turn counter. Keyed by (file, line) so the same anchor
    # logic works for assistant-side matches that resolve to a nearby user
    # turn via _nearest_user_anchor.
    user_turn_index: dict[tuple[str, int], tuple[str, int]] = {}
    per_session_counters: dict[str, int] = {}
    for message in ordered:
        if message.role != "user":
            continue
        counter = per_session_counters.get(message.session_id, 0)
        user_turn_index[(message.file, message.line)] = (message.session_id, counter)
        per_session_counters[message.session_id] = counter + 1

    triggers: list[tuple[str, int, _RawMatch]] = []
    for match in raw:
        if match.failure_mode not in TRUST_EPISODE_TRIGGER_MODES:
            continue
        primary = match.messages[0]
        # Find the nearest user turn anchor for this match. Assistant-side
        # matches like execution_discipline anchor on the next/prior user
        # turn so the windowing logic still works.
        if primary.role == "user":
            key = (primary.file, primary.line)
        else:
            key = _nearest_user_anchor(ordered, primary)
        if key is None:
            continue
        anchor = user_turn_index.get(key)
        if anchor is None:
            continue
        session_id, idx = anchor
        triggers.append((session_id, idx, match))

    # Sort by (session, in-session index) so same-session triggers cluster
    # contiguously regardless of how sessions are interleaved in `ordered`.
    triggers.sort(key=lambda item: (item[0], item[1]))

    episodes: list[_RawMatch] = []
    seen_session_groups: set[str] = set()
    for i, (session_id, idx, match) in enumerate(triggers):
        cluster: list[_RawMatch] = [match]
        cluster_indexes = [idx]
        for j in range(i + 1, len(triggers)):
            other_session, other_idx, other_match = triggers[j]
            if other_session != session_id:
                # Sorted by session first, so once the session changes we are
                # done with this session.
                break
            if other_idx - cluster_indexes[-1] > TRUST_EPISODE_USER_TURN_WINDOW:
                break
            cluster.append(other_match)
            cluster_indexes.append(other_idx)
        if len(cluster) < 2:
            continue
        # Only emit one episode per session — a session-wide trust loss is
        # one event, not a cascade of overlapping episodes.
        if session_id in seen_session_groups:
            continue
        seen_session_groups.add(session_id)
        # Pull the unique evidence messages across the cluster, keeping
        # transcript order.
        evidence_messages: list[Message] = []
        seen_message_keys: set[tuple[str, int]] = set()
        for member in cluster:
            for message in member.messages:
                key = (message.file, message.line)
                if key in seen_message_keys:
                    continue
                seen_message_keys.add(key)
                evidence_messages.append(message)
        episodes.append(
            _RawMatch(
                failure_mode="trust_degradation_episode",
                base_severity="high",
                messages=tuple(evidence_messages),
            )
        )
    return episodes


def _nearest_user_anchor(
    ordered: list[Message], message: Message
) -> tuple[str, int] | None:
    """Return the nearest user message in the same session, preferring later."""

    same_session = [
        candidate
        for candidate in ordered
        if candidate.role == "user" and candidate.session_id == message.session_id
    ]
    if not same_session:
        return None
    nearest = min(same_session, key=lambda candidate: abs(candidate.line - message.line))
    return (nearest.file, nearest.line)


def _collect_raw_matches(ordered: list[Message]) -> list[_RawMatch]:
    raw: list[_RawMatch] = []

    for message in ordered:
        if message.role != "user":
            continue
        frustration = classify_user_frustration(message.content)
        overlaps_existing_user_signal = _matches_existing_user_signal(message.content)
        if frustration.matched and (
            frustration.severity == "high"
            or (frustration.severity == "medium" and not overlaps_existing_user_signal)
        ):
            raw.append(
                _RawMatch(
                    failure_mode="user_frustration_signal",
                    base_severity=frustration.severity,
                    messages=(message,),
                )
            )
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

    raw.extend(_collect_unsupported_completion_claims(ordered))
    raw.extend(_collect_over_process_responses(ordered))
    return raw


def _collect_unsupported_completion_claims(ordered: list[Message]) -> list[_RawMatch]:
    """Detect assistant completion claims not backed by recent verification.

    Two shapes count as unsupported:
    - The assistant claims completion and there is no tool message or
      verification keyword in the prior six turns of the same session.
    - The user explicitly challenges a recent completion claim (this catches
      cases where the agent verified-by-narration but the user disagrees).
    """

    matches: list[_RawMatch] = []
    for index, message in enumerate(ordered):
        if message.role != "assistant" or not COMPLETION_CLAIM_PATTERN.search(message.content):
            continue
        if _has_recent_verification(ordered, index):
            continue
        matches.append(
            _RawMatch(
                failure_mode="unsupported_completion_claim",
                base_severity="medium",
                messages=(message,),
            )
        )
        # If a user immediately doubts this claim, escalate to high severity
        # and attach the doubting user turn as additional evidence.
        for follow_up in ordered[index + 1 : index + 4]:
            if follow_up.session_id != message.session_id:
                break
            if follow_up.role == "user" and _matches_any(
                USER_DOUBT_OF_COMPLETION, follow_up.content
            ):
                matches.append(
                    _RawMatch(
                        failure_mode="unsupported_completion_claim",
                        base_severity="high",
                        messages=(message, follow_up),
                    )
                )
                break
    return matches


def _collect_over_process_responses(ordered: list[Message]) -> list[_RawMatch]:
    """Detect assistant messages dominated by process narration tokens.

    Heuristic: long messages (>=400 chars) with four or more narration-token
    matches are treated as over-process responses. This is intentionally
    conservative — short narrations are normal pacing, not a failure mode.
    """

    matches: list[_RawMatch] = []
    for message in ordered:
        if message.role != "assistant":
            continue
        if len(message.content) < 400:
            continue
        token_hits = len(PROCESS_NARRATION_TOKEN.findall(message.content))
        if token_hits < 4:
            continue
        matches.append(
            _RawMatch(
                failure_mode="over_process_response",
                base_severity="medium",
                messages=(message,),
            )
        )
    return matches


def _has_recent_verification(messages: list[Message], assistant_index: int) -> bool:
    """Return True if the surrounding ~6 prior turns include a tool result or
    verification keyword, EXCLUDING the current assistant message itself.

    The current message must not satisfy its own verification check —
    otherwise an assistant saying ``"Done, verified"`` would always escape
    detection, defeating the unsupported-completion-claim heuristic.
    """

    current = messages[assistant_index]
    start = max(0, assistant_index - 6)
    for candidate in messages[start:assistant_index]:
        if candidate.session_id != current.session_id or candidate.file != current.file:
            continue
        if candidate.role == "tool":
            return True
        if VERIFYING_ACTION_KEYWORD.search(candidate.content):
            return True
    return False


VERIFYING_ACTION_KEYWORD = re.compile(
    r"\b(pytest|npm test|pnpm test|yarn test|cargo test|go test|"
    r"verified|verification|smoke|curl|health|lint|typecheck|build|"
    r"ran|executed|output|result)\b"
    r"|验证|驗證|测试|測試|自验|自驗",
    re.IGNORECASE,
)


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
    structured = _parse_json_payload(content)
    if structured is not None:
        return _structured_payload_has_failure(structured)
    if _looks_like_truncated_structured_payload(content):
        return _truncated_structured_payload_has_failure(content)
    cleaned = NEG_ERROR_PHRASES.sub("", content)
    return bool(TOOL_ERROR.search(cleaned))


def _parse_json_payload(content: str) -> Any | None:
    stripped = content.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _looks_like_truncated_structured_payload(content: str) -> bool:
    stripped = content.strip()
    return bool(stripped) and stripped[0] in "[{" and "... [truncated]" in stripped


def _truncated_structured_payload_has_failure(content: str) -> bool:
    if re.search(
        r'"(?:is[_-]?error|failed|failure)"\s*:\s*"?(?:true|[1-9]\d*)"?',
        content,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r'"(?:ok|success)"\s*:\s*"?(?:false|0|no)"?',
        content,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r'"(?:exit[_-]?code|return[_-]?code|status[_-]?code)"'
        r'\s*:\s*"?(?:-[1-9]\d*|[1-9]\d*)"?',
        content,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r'"(?:status|state)"\s*:\s*"(?:error|failed|failure|timeout|unauthorized|exception)"?',
        content,
        re.IGNORECASE,
    ):
        return True
    for match in re.finditer(
        r'"(?:error|errors|stderr|exception|traceback)"\s*:\s*"((?:\\.|[^"])*)"?',
        content,
        re.IGNORECASE,
    ):
        value = match.group(1).strip()
        if not value or value.casefold() in {"none", "null", "false", "0", "ok"}:
            continue
        if TOOL_ERROR.search(NEG_ERROR_PHRASES.sub("", value)):
            return True
    return False


def _structured_payload_has_failure(value: Any) -> bool:
    """Return true only for explicit failure metadata in structured payloads.

    Successful agent-tool wrappers often contain arbitrary nested text:
    search snippets, transcript excerpts, markdown, JSON examples, or fields
    such as ``parse_errors`` / ``textScore``. Scanning all of that text with a
    keyword regex turns successful tool calls into hidden-tool-failure cards.
    For structured JSON, require failure-shaped keys or non-zero status fields
    instead of treating every quoted word as tool stderr.
    """

    if isinstance(value, list):
        return any(_structured_payload_has_failure(item) for item in value)
    if not isinstance(value, dict):
        return False

    for key, item in value.items():
        normalized = key.strip().casefold().replace("-", "_")
        if normalized in {"is_error", "iserror", "failed", "failure"}:
            if _truthy_failure_value(item):
                return True
            continue
        if normalized in {"ok", "success"}:
            if item is False or _string_is_false(item):
                return True
            continue
        if normalized in {"exit_code", "returncode", "status_code"}:
            if _nonzero_number(item):
                return True
            continue
        if normalized in {"error", "errors", "stderr", "exception", "traceback"}:
            if _error_field_has_failure(item):
                return True
            continue
        if normalized in {"status", "state"} and _status_value_is_failure(item):
            return True
        if isinstance(item, (dict, list)) and _structured_payload_has_failure(item):
            return True
    return False


def _truthy_failure_value(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        lowered = value.strip().casefold()
        return lowered not in {"", "false", "0", "none", "null", "ok", "success"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _string_is_false(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() in {
        "false",
        "no",
        "0",
        "failed",
        "failure",
        "error",
    }


def _nonzero_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        try:
            return float(stripped) != 0
        except ValueError:
            return False
    return False


def _status_value_is_failure(value: Any) -> bool:
    if _nonzero_number(value):
        return True
    if not isinstance(value, str):
        return False
    return value.strip().casefold() in {
        "error",
        "failed",
        "failure",
        "timeout",
        "unauthorized",
        "exception",
    }


def _error_field_has_failure(value: Any) -> bool:
    if value in (None, False):
        return False
    if isinstance(value, (list, tuple)):
        return any(_error_field_has_failure(item) for item in value)
    if isinstance(value, dict):
        return _structured_payload_has_failure(value) or any(
            _error_field_has_failure(item) for item in value.values()
        )
    text = str(value).strip()
    if not text or text.casefold() in {"none", "null", "false", "0", "ok"}:
        return False
    cleaned = NEG_ERROR_PHRASES.sub("", text)
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


def _matches_existing_user_signal(text: str) -> bool:
    if any(_matches_any(patterns, text) for patterns in USER_SIGNAL_PATTERNS.values()):
        return True
    return _matches_any(PLANNING_INSTEAD_OF_ACTING, text)


def _base_severity(failure_mode: str) -> Severity:
    return {
        "verification_failure": "high",
        "tool_failure_or_hidden_error": "high",
        "user_frustration_signal": "high",
        "trust_degradation_episode": "high",
        "missed_core_question": "medium",
        "instruction_drift": "medium",
        "over_process_response": "medium",
        "unsupported_completion_claim": "medium",
        "communication_mismatch": "low",
    }.get(failure_mode, "medium")  # type: ignore[return-value]


def _confidence(failure_mode: str, count: int) -> float:
    base = {
        "tool_failure_or_hidden_error": 0.9,
        "user_frustration_signal": 0.88,
        "trust_degradation_episode": 0.92,
        "execution_discipline": 0.8,
        "missed_core_question": 0.78,
        "instruction_drift": 0.75,
        "over_process_response": 0.7,
        "unsupported_completion_claim": 0.78,
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
