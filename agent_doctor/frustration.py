"""Local user-frustration classifier for Agent Doctor.

This module is deliberately deterministic. It is the fast wake-up layer for
obvious user anger, insults, trust-break language, and repeated correction
signals; deeper LLM diagnosis remains an opt-in evaluation path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import Severity


@dataclass(frozen=True)
class FrustrationSignal:
    matched: bool
    severity: Severity = "low"
    score: int = 0
    labels: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""


PROFANITY_OR_INSULT = re.compile(
    r"\b("
    r"fuck|fucking|wtf|bullshit|shit|idiot|moron|clown"
    r")\b"
    r"|\b(?:this|that|your|the)\s+(?:agent|answer|response|result|output|thing)\s+"
    r"(?:is\s+)?(?:stupid|useless|worthless|pathetic)\b"
    r"|\bthis\s+is\s+(?:stupid|useless|worthless|pathetic)\b"
    r"|\b(?:stupid|useless|worthless|pathetic)\s+(?:agent|answer|response|result|output)\b"
    r"|\bdumb\s+(?:agent|answer|response|idea|thing)\b"
    r"|\b(?:this|that|your|the)\s+(?:agent|answer|response|result|output)\s+"
    r"(?:is\s+)?(?:garbage|trash)\b"
    r"|\b(?:garbage|trash)\s+(?:agent|answer|response|result|output)\b"
    r"|傻逼|傻[屌吊]|蠢货|蠢貨|废物|廢物|垃圾(?!回收|桶|箱|分类|分類|文件|目录|目錄|数据|資料)"
    r"|滚(?:吧|开|蛋|$|[！!。.?？\s])|滾(?:吧|開|蛋|$|[！!。.?？\s])|脑子有病|腦子有病",
    re.IGNORECASE,
)

DIRECT_QUALITY_COMPLAINT = re.compile(
    r"\b("
    r"not smart|less smart|not intelligent|not useful|no value|"
    r"not thinking|haven'?t thought|didn'?t think|"
    r"what are you doing|what the hell|same mistake|wrong again"
    r")\b"
    r"|不够聪明|不夠聰明|不聪明|不聰明|没用|沒有用|没价值|沒有价值|"
    r"有没有想清楚|有沒有想清楚|你到底|为什么没有用|為什麼沒有用|"
    r"你在干嘛|你在幹嘛|你是不是没想|你是不是沒想|不要搞偏|随风倒|隨風倒"
    r"|你(?:怎么|怎麼)?(?:这么|這麼|那么|那麼|很)?笨"
    r"|(?:这么|這麼|那么|那麼|太|真|很)笨|笨蛋|笨死",
    re.IGNORECASE,
)

TRUST_BREAK = re.compile(
    r"\b("
    r"can'?t trust you|cannot trust you|don'?t trust you|lost trust|"
    r"i give up|this keeps happening|you keep doing this"
    r")\b"
    r"|不能相信你|不信任你|信任崩|又来了|又來了|一直这样|一直這樣|"
    r"每次都这样|每次都這樣|你又错了|你又錯了|你又搞错|你又搞錯",
    re.IGNORECASE,
)

REPEATED_CORRECTION = re.compile(
    r"\b("
    r"i already told you|i said this already|not what i asked|"
    r"you did it again|missed it again"
    r")\b"
    r"|我已经说过|我已經說過|不是这个|不是這個|不是我要的|又不是"
    r"|你错了|你錯了|你说错了|你說錯了",
    re.IGNORECASE,
)

AMBIGUOUS_SUPPORTING_SIGNAL = re.compile(
    r"\b(do you understand|same problem)\b",
    re.IGNORECASE,
)


def classify_user_frustration(text: str) -> FrustrationSignal:
    """Classify whether a user message is a product-relevant frustration signal."""

    labels: list[str] = []
    score = 0

    if PROFANITY_OR_INSULT.search(text):
        labels.append("profanity_or_insult")
        score += 3
    if TRUST_BREAK.search(text):
        labels.append("trust_break")
        score += 3
    if DIRECT_QUALITY_COMPLAINT.search(text):
        labels.append("direct_quality_complaint")
        score += 3
    if REPEATED_CORRECTION.search(text):
        labels.append("repeated_correction")
        score += 1
    if AMBIGUOUS_SUPPORTING_SIGNAL.search(text):
        labels.append("ambiguous_supporting_signal")
        score += 1
    if _has_urgency_shape(text):
        labels.append("high_urgency_shape")
        score += 1

    if score >= 3:
        severity: Severity = "high"
    elif score >= 2:
        severity = "medium"
    elif score >= 1:
        severity = "low"
    else:
        return FrustrationSignal(matched=False)

    rationale = ", ".join(labels)
    return FrustrationSignal(
        matched=True,
        severity=severity,
        score=score,
        labels=tuple(labels),
        rationale=rationale,
    )


def _has_urgency_shape(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _looks_like_technical_token(stripped):
        return False
    if len(re.findall(r"[!?！？]", stripped)) >= 2:
        return True
    letters = re.findall(r"[A-Z]", stripped)
    alpha = re.findall(r"[A-Za-z]", stripped)
    return len(alpha) >= 8 and len(letters) / max(1, len(alpha)) >= 0.75


def _looks_like_technical_token(text: str) -> bool:
    token = text.strip("!?！？`'\"")
    if not token or re.search(r"\s", token):
        return False
    if "_" in token or "/" in token:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", token))
