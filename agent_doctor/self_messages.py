"""Detection guards for Agent Doctor's own recovery prompts."""

from __future__ import annotations


_SELF_RECOVERY_MARKERS = (
    "agent_doctor_intervention",
    "agent doctor detected a live quality issue",
    "agent doctor 检测到当前 openclaw 会话出现质量/信任问题",
)


def is_agent_doctor_recovery_message(text: str) -> bool:
    normalized = text.casefold()
    return any(marker in normalized for marker in _SELF_RECOVERY_MARKERS)
