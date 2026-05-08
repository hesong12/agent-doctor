"""Context-aware comfort copy for the lightweight Agent Doctor surface."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable

from .redaction import redact_text
from .schema import Message

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComfortCopy:
    headline: str
    message: str
    mood: str
    source: str = "fallback"


def comfort_copy_for_event(
    *,
    trigger: str,
    severity: str,
    evidence: str,
    recent: list[Message],
    chinese: bool,
    generator: Callable[[str], str] | None = None,
) -> ComfortCopy:
    """Build short scene-aware comfort text without changing detector behavior.

    A host model is the product path: the copy must be written from the
    current scene, not pulled from canned phrases. The deterministic branch is
    only a degradation path for local auth/network/model failures.
    """

    quote = _latest_user_quote(recent) or _clean(evidence)
    assistant = _latest_assistant_quote(recent)
    if generator is not None:
        generated = _generated_copy(
            generator=generator,
            trigger=trigger,
            severity=severity,
            evidence=evidence,
            recent=recent,
            quote=quote,
            assistant=assistant,
            chinese=chinese,
        )
        if generated is not None:
            return generated
    theme = _theme(trigger, quote + "\n" + assistant, chinese=chinese)
    variant = _variant(trigger, severity, quote, assistant, size=4)
    if chinese:
        return _zh_copy(theme, quote, assistant, variant)
    return _en_copy(theme, quote, assistant, variant)


def _generated_copy(
    *,
    generator: Callable[[str], str],
    trigger: str,
    severity: str,
    evidence: str,
    recent: list[Message],
    quote: str,
    assistant: str,
    chinese: bool,
) -> ComfortCopy | None:
    prompt = _comfort_prompt(
        trigger=trigger,
        severity=severity,
        evidence=evidence,
        recent=recent,
        quote=quote,
        assistant=assistant,
        chinese=chinese,
    )
    try:
        raw = generator(prompt)
    except Exception as exc:  # pragma: no cover - exercised through callers
        _log.info("comfort generation failed; using fallback copy: %s", exc)
        return None
    payload = _parse_json_object(raw)
    if payload is None:
        _log.info("comfort generation returned non-JSON output; using fallback copy")
        return None
    headline = _clean(str(payload.get("headline") or ""))
    message = _clean(str(payload.get("message") or ""))
    mood = _clean(str(payload.get("mood") or "")) or _theme(
        trigger,
        quote + "\n" + assistant,
        chinese=chinese,
    )
    if not headline or not message:
        return None
    if _looks_generic(message, quote=quote, assistant=assistant):
        _log.info("comfort generation was too generic; using fallback copy")
        return None
    return ComfortCopy(
        headline=_clip_quote(headline, 36 if chinese else 48),
        message=_clip_quote(message, 180 if chinese else 240),
        mood=mood,
        source="model",
    )


def _comfort_prompt(
    *,
    trigger: str,
    severity: str,
    evidence: str,
    recent: list[Message],
    quote: str,
    assistant: str,
    chinese: bool,
) -> str:
    language = "Chinese" if chinese else "English"
    compact_recent = [
        {
            "role": message.role,
            "text": _clip_quote(message.content, 360),
        }
        for message in recent[-8:]
        if message.role in {"user", "assistant", "tool"}
    ]
    scene = {
        "trigger": trigger,
        "severity": severity,
        "evidence": _clip_quote(evidence, 500),
        "latest_user_quote": _clip_quote(quote, 240),
        "latest_assistant_quote": _clip_quote(assistant, 240),
        "recent_messages": compact_recent,
    }
    return (
        "You write the tiny Agent Doctor pet's comfort copy after it detects "
        "that the user is upset with an AI agent.\n"
        f"Write in {language}. Use the actual scene below. Do not use generic therapy, "
        "do not diagnose, do not tell the user to take actions, do not mention templates, "
        "and do not defend the agent. Be warm, specific, a little playful, and concise. "
        "The message must clearly react to the latest user quote and the immediate agent/tool context. "
        "Return only minified JSON with keys headline, message, mood. "
        "headline <= 18 Chinese chars or <= 8 English words. message <= 2 sentences.\n"
        f"Scene JSON: {json.dumps(scene, ensure_ascii=False, separators=(',', ':'))}"
    )


def _parse_json_object(raw: str) -> dict[str, object] | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _looks_generic(message: str, *, quote: str, assistant: str) -> bool:
    anchors = _anchor_terms(quote) + _anchor_terms(assistant)
    if not anchors:
        return False
    lowered = message.casefold()
    return not any(anchor.casefold() in lowered for anchor in anchors)


def _anchor_terms(text: str) -> list[str]:
    cleaned = _clean(text)
    if not cleaned:
        return []
    chinese_terms = []
    for term in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
        if len(term) <= 4:
            chinese_terms.append(term)
            continue
        chinese_terms.extend(term[index : index + 2] for index in range(0, len(term) - 1))
    english_terms = [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", cleaned)
        if token.casefold()
        not in {
            "that",
            "this",
            "with",
            "what",
            "have",
            "your",
            "youre",
            "just",
            "from",
            "they",
            "were",
            "been",
            "will",
            "would",
            "could",
            "should",
            "agent",
        }
    ]
    return chinese_terms[:40] + english_terms[:12]


def _zh_copy(theme: str, quote: str, assistant: str, variant: int) -> ComfortCopy:
    short_quote = _clip_quote(quote, 30)
    short_assistant = _clip_quote(assistant, 26)
    if theme == "tool":
        lines = [
            f"我看到工具这一步把事情卡住了，不是你挑剔，是它真的没把结果交代清楚。小医生先抱着日志蹦两下，把这口气接住。",
            f"这里像是工具/执行环节掉链子了。你不用替它补脑，Agent Doctor 先把失败感接住。",
            f"工具失败这种不透明最烦人。小医生已经把那段错误抱起来了，先让你不用再盯着一坨输出生气。",
            f"这次不是一句“好了”能糊过去的工具卡点。小医生先在旁边举灯，把失败的地方照出来。",
        ]
        headline = "工具卡住了，我看到了"
    elif theme == "verification":
        lines = [
            f"它像是太早说完成了。你不需要接受这种没证据的“搞定”，小医生先把验证这件事按住。",
            f"这里的问题是信心跑在证据前面了。先别急着被它带走，小医生已经把这一步拦下来了。",
            f"这个“完成感”不够踏实。小医生先挥挥小旗，提醒它拿证据说话。",
            f"它刚才像是跳过了验证。小医生先帮你把节奏降下来，不让这事混过去。",
        ]
        headline = "先别信它说完成"
    elif theme == "offtrack":
        lines = [
            f"我看到你在说它没抓住重点。你不是在找情绪宣泄，你是在要它回到真正的问题上。",
            f"这里像是答偏了。小医生先把方向盘抱住，别让它继续往旁边开。",
            f"你这句不是普通抱怨，是在提醒它核心没对齐。小医生先把这个信号亮出来。",
            f"它可能又把问题做成了别的问题。小医生先挡一下，别让你再重复解释一遍。",
        ]
        headline = "它可能答偏了"
    else:
        lines = [
            f"我看到你刚才这句“{short_quote}”。先不用压着火，小医生先跳出来陪你一下，再让事情回到正轨。",
            f"这一下确实会让人烦。小医生先把这口气接住，别让你还得反过来照顾 agent 的感受。",
            f"我看到了，不是你小题大做。小医生先在旁边摇铃：这里需要被认真对待。",
            f"收到，这不是普通提醒，是信任被磨到了。小医生先贴过来陪你一下。",
        ]
        headline = "我看到你不爽了"
    message = lines[variant]
    if short_quote and short_quote not in message:
        message += f" 我看到的是“{short_quote}”。"
    if assistant and theme in {"offtrack", "verification"}:
        message += f" 刚才那句“{short_assistant}”会被当作现场背景。"
    return ComfortCopy(headline=headline, message=message, mood=theme)


def _en_copy(theme: str, quote: str, assistant: str, variant: int) -> ComfortCopy:
    short_quote = _clip_quote(quote, 34)
    short_assistant = _clip_quote(assistant, 30)
    if theme == "tool":
        lines = [
            "I can see the tool step made this messy. You should not have to reverse-engineer a failure from noisy output.",
            "That looks like an execution snag, not you being picky. The little doctor is catching the frustration before it spills over.",
            "Tool failures are especially annoying when they are vague. I am keeping the spotlight on the broken step.",
            "This is not a moment for a fake all-good answer. The pet is waving a tiny flag over the failed step.",
        ]
        headline = "The tool path got messy"
    elif theme == "verification":
        lines = [
            "It looks like the answer got ahead of the evidence. You do not have to accept a confident claim without proof.",
            "The confidence came too early here. I am slowing the moment down so verification has to come first.",
            "That completion signal does not feel earned yet. The pet is tapping the brakes.",
            "It seems to have skipped the proof step. I am keeping that visible instead of letting it slide.",
        ]
        headline = "The proof is missing"
    elif theme == "offtrack":
        lines = [
            "I can see this is about the agent missing the point, not just tone. The pet is nudging it back toward the real ask.",
            "This feels off-track. I am holding the thread in place so you do not have to explain it again.",
            "That was not just irritation; it was a signal that the answer drifted. I am marking that clearly.",
            "The agent may have solved a neighboring problem. The pet is gently blocking that detour.",
        ]
        headline = "The answer drifted"
    else:
        lines = [
            f"I saw “{short_quote}.” You do not need to soften that; the pet is here to absorb the heat for a second.",
            "That was frustrating. The little doctor is stepping in with a tiny bounce before we put the work back on track.",
            "I see it. This is not you overreacting; this is a trust moment that deserves care.",
            "Got it. The pet is here to make the moment less sharp before the session continues.",
        ]
        headline = "I saw the frustration"
    message = lines[variant]
    if short_quote and short_quote not in message:
        message += f" I saw “{short_quote}.”"
    if assistant and theme in {"offtrack", "verification"}:
        message += f" I am treating “{short_assistant}” as part of the scene."
    return ComfortCopy(headline=headline, message=message, mood=theme)


def _theme(trigger: str, text: str, *, chinese: bool) -> str:
    if trigger == "tool_failure_or_hidden_error" or re.search(
        r"\b(error|failed|failure|exception|traceback|timeout)\b|失败|錯誤|错误|异常",
        text,
        re.I,
    ):
        return "tool"
    if trigger == "completion_claim_without_nearby_verification" or re.search(
        r"\b(done|completed|fixed|verified|passed)\b|完成了|搞定了|修好了|已验证",
        text,
        re.I,
    ):
        return "verification"
    if re.search(r"not what i asked|missed|off.?track|不是我要|不要搞偏|答偏|搞错|搞錯", text, re.I):
        return "offtrack"
    return "frustration"


def _latest_user_quote(recent: list[Message]) -> str:
    return next((_clean(message.content) for message in reversed(recent) if message.role == "user"), "")


def _latest_assistant_quote(recent: list[Message]) -> str:
    return next((_clean(message.content) for message in reversed(recent) if message.role == "assistant"), "")


def _clean(text: str) -> str:
    text = re.sub(
        r"(Conversation info|Sender) \(untrusted metadata\):\s*```json.*?```\s*",
        "",
        text.strip(),
        flags=re.S,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return redact_text(text)


def _clip_quote(text: str, limit: int) -> str:
    cleaned = _clean(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _variant(*parts: str, size: int) -> int:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).digest()
    return digest[0] % size
