"""Multi-signal fusion for frustration detection.

Three deterministic, no-LLM signals that complement the regex tier:

1. Typing-shape: punctuation density, ALL-CAPS bursts, length spike/collapse.
2. Trajectory: escalation pattern across last N user messages.
3. Repeat-theme: same word/concept repeated across turns.

Each signal returns a 0-3 score added to the regex score for the final decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SignalScores:
    """Per-signal contributions to fused frustration score."""
    typing_shape: int = 0
    trajectory: int = 0
    repeat_theme: int = 0

    @property
    def total(self) -> int:
        return self.typing_shape + self.trajectory + self.repeat_theme


# --- typing-shape ------------------------------------------------------------


def score_typing_shape(text: str) -> int:
    """0-2 points based on punctuation/caps density.

    +1: 3+ exclamations or question marks (any mix)
    +1: 4+ alpha chars where >=75% are uppercase (English-style yelling)
    """
    score = 0
    punct = len(re.findall(r"[!?！？]", text))
    if punct >= 3:
        score += 1
    alpha = re.findall(r"[A-Za-z]", text)
    upper = re.findall(r"[A-Z]", text)
    if len(alpha) >= 4 and len(upper) / max(1, len(alpha)) >= 0.75:
        score += 1
    return score


# --- trajectory --------------------------------------------------------------


def score_trajectory(messages: list[str], window: int = 5) -> int:
    """0-2 points based on escalation across last N user messages.

    +1: monotonic length growth or shrinkage (first half avg vs second half)
    +1: increasing punctuation density across the window
    """
    if len(messages) < 3:
        return 0
    recent = messages[-window:]
    half = len(recent) // 2
    if half < 1:
        return 0
    first_half = recent[:half]
    second_half = recent[half:]

    score = 0
    avg1_len = sum(len(m) for m in first_half) / max(1, len(first_half))
    avg2_len = sum(len(m) for m in second_half) / max(1, len(second_half))
    # Length growth >= 50% or shrinkage >= 50% counts as escalation
    if avg2_len >= 1.5 * avg1_len or avg2_len <= 0.5 * avg1_len:
        score += 1

    avg1_punct = sum(len(re.findall(r"[!?！？]", m)) for m in first_half) / max(1, len(first_half))
    avg2_punct = sum(len(re.findall(r"[!?！？]", m)) for m in second_half) / max(1, len(second_half))
    if avg2_punct > avg1_punct + 0.5:  # noticeable jump
        score += 1
    return score


# --- repeat-theme ------------------------------------------------------------


_EN_TOKENIZE = re.compile(r"[A-Za-z]{3,}")
_CJK_RUN = re.compile(r"[一-鿿]{2,}")

_EN_STOPWORDS = {
    "the",
    "and",
    "are",
    "you",
    "this",
    "that",
    "with",
    "for",
    "from",
    "what",
    "why",
    "how",
    "can",
    "could",
    "would",
    "should",
}

_CJK_STOPWORDS = {
    "一个",
    "一些",
    "这个",
    "那个",
    "我们",
    "你们",
    "他们",
    "它们",
    "可以",
    "需要",
    "就是",
    "不是",
    "这个",
    "事情",
    "东西",
    "本身",
    "目前",
    "更加",
    "一下",
    "了吗",
    "哪里",
    "什么",
    "怎么",
    "为什么",
}


def score_repeat_themes(messages: list[str], min_repeats: int = 3) -> int:
    """+1 if a content token appears in >= min_repeats distinct user turns.

    Repeat-theme is a trajectory signal, not a density signal. Counting raw
    occurrences inside one long Chinese question makes normal inquiry look like
    frustration, so each message contributes a token at most once and CJK text is
    represented by short phrases rather than single common characters.
    """
    message_counts: dict[str, int] = {}
    for message in messages[-10:]:
        for token in _content_tokens(message):
            message_counts[token] = message_counts.get(token, 0) + 1
    return 1 if message_counts and max(message_counts.values()) >= min_repeats else 0


def _content_tokens(message: str) -> set[str]:
    tokens: set[str] = set()
    for token in _EN_TOKENIZE.findall(message.lower()):
        if token not in _EN_STOPWORDS:
            tokens.add(token)
    for run in _CJK_RUN.findall(message):
        if run in _CJK_STOPWORDS:
            continue
        for size in (2, 3):
            for index in range(0, len(run) - size + 1):
                token = run[index : index + size]
                if token not in _CJK_STOPWORDS:
                    tokens.add(token)
    return tokens


# --- fusion -----------------------------------------------------------------


def fuse_signals(
    *,
    text: str,
    recent_user_messages: list[str] | None = None,
) -> SignalScores:
    """Compute all three signals and return the breakdown.

    `text` is the current user message; `recent_user_messages` includes it
    plus prior user messages in the same session (most-recent last).
    """
    typing = score_typing_shape(text)
    trajectory = 0
    repeat = 0
    if recent_user_messages:
        trajectory = score_trajectory(recent_user_messages)
        repeat = score_repeat_themes(recent_user_messages)
    return SignalScores(
        typing_shape=typing,
        trajectory=trajectory,
        repeat_theme=repeat,
    )
