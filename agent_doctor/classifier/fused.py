"""Fused classifier: orchestrates Tier 1 regex + signal fusion + user dict + Tier 2 LLM.

Used by autopilot's detect_autopilot_events. Designed to be cheap on the
common path:
- Tier 1 always runs (regex)
- Signal fusion always runs (cheap deterministic)
- User dict applies if loaded (no I/O when None)
- Tier 2 runs ONLY when (a) Tier 1 score is borderline AND (b) adapter
  has can_infer_text=True. Cap and cache come from tier2.tier2_classify.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..frustration import FrustrationSignal, classify_user_frustration
from ..schema import Severity
from .signal_fusion import fuse_signals
from .tier2 import Tier2Result, tier2_classify
from .user_dict import UserDict


_SEMANTIC_ANCHOR_LABELS = {
    "profanity_or_insult",
    "trust_break",
    "direct_quality_complaint",
}


def fused_classify(
    text: str,
    *,
    recent_user_messages: Optional[list[str]] = None,
    user_dict: Optional[UserDict] = None,
    adapter=None,  # HostAdapter | None
    tier2_cache_path: Optional[Path] = None,
    tier2_model: Optional[str] = None,
    tier2_max_calls_per_day: int = 100,
) -> FrustrationSignal:
    """Return a fused FrustrationSignal across all available tiers.

    Tier 1 is always called; signal fusion always; user_dict if provided;
    Tier 2 only when borderline AND adapter supports infer.
    """
    # Tier 1
    tier1 = classify_user_frustration(text)
    score = tier1.score
    labels = list(tier1.labels)
    rationale_parts: list[str] = []
    if tier1.rationale:
        rationale_parts.append(f"tier1={tier1.rationale}")
    has_semantic_anchor = _has_semantic_anchor(labels)

    # Signal fusion (always), but do not let purely contextual fusion
    # turn neutral acknowledgements ("ok", "好了", "继续") into incidents.
    if tier1.score == 0 and _is_neutral_acknowledgement(text):
        recent_for_fusion: list[str] = [text]
    else:
        recent_for_fusion = recent_user_messages or []
    fusion = fuse_signals(text=text, recent_user_messages=recent_for_fusion)
    if fusion.total > 0:
        score += fusion.total
        if fusion.typing_shape:
            labels.append("typing_shape")
        if fusion.trajectory:
            labels.append("trajectory_escalation")
        if fusion.repeat_theme:
            labels.append("repeat_theme")
        rationale_parts.append(f"fusion={fusion.total}")

    # User dict (optional)
    if user_dict is not None:
        adj = user_dict.score_adjustment(text)
        if adj != 0:
            score += adj
            labels.append(f"user_dict_{'positive' if adj > 0 else 'negative'}")
            rationale_parts.append(f"user_dict={adj:+d}")
            if adj > 0:
                has_semantic_anchor = True

    # Tier 2 (borderline only, opt-in by adapter capability)
    if adapter is not None and 1 <= score <= 2:
        try:
            caps = adapter.capabilities()
            if caps.can_infer_text:
                t2 = tier2_classify(
                    text,
                    adapter,
                    cache_path=tier2_cache_path,
                    model=tier2_model,
                    max_calls_per_day=tier2_max_calls_per_day,
                )
                if not t2.skipped:
                    # Merge: Tier 2 severity replaces if higher
                    t2_severity_score = _severity_to_score(t2.severity)
                    if t2_severity_score > score:
                        score = t2_severity_score
                    for s in t2.signals:
                        labels.append(f"tier2_{s}")
                    if t2.severity in {"medium", "high"}:
                        has_semantic_anchor = True
                    rationale_parts.append(f"tier2={t2.severity}({t2.rationale})")
        except Exception as exc:
            rationale_parts.append(f"tier2_error={exc}")

    if not has_semantic_anchor:
        return FrustrationSignal(matched=False)

    severity = _score_to_severity(score)
    if severity is None:
        return FrustrationSignal(matched=False)
    return FrustrationSignal(
        matched=True,
        severity=severity,
        score=score,
        labels=tuple(labels),
        rationale="; ".join(rationale_parts),
    )


def _severity_to_score(severity: str) -> int:
    return {"none": 0, "low": 1, "medium": 2, "high": 3}.get(severity, 0)


def _score_to_severity(score: int) -> Optional[Severity]:
    if score >= 3:
        return "high"
    if score == 2:
        return "medium"
    if score == 1:
        return "low"
    return None


def _is_neutral_acknowledgement(text: str) -> bool:
    normalized = text.strip().casefold().strip(".!?。！？…~～ ")
    return normalized in {
        "ok",
        "okay",
        "k",
        "继续",
        "繼續",
        "好了",
        "好",
        "行",
        "可以",
        "go on",
        "continue",
    }


def _has_semantic_anchor(labels: list[str]) -> bool:
    return any(label in _SEMANTIC_ANCHOR_LABELS for label in labels)
