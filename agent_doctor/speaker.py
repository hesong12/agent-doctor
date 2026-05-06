"""Speaker: render structured 🩺 messages.

Five templates: intervene, propose, digest, applied, undone.

Localization is per-message via the `language` arg (typically derived
from `SessionMetadata.language` by the channel router). Today supports
"en" and "zh"; other languages fall through to "en".

Templates intentionally produce plain text. Channel-specific formatting
(markdown, HTML) is the adapter's job, not the speaker's.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal

from .adapters import MessageBody
from .autopilot import AutopilotEvent

Language = Literal["en", "zh"]


# --- text-bundle helpers -----------------------------------------------------


def _t(language: str, en: str, zh: str) -> str:
    """Pick the localized string. Fallback to en for unknown languages."""
    if language == "zh":
        return zh
    return en


# --- templates ---------------------------------------------------------------


def render_intervene(event: AutopilotEvent, *, language: str = "en") -> MessageBody:
    """🩺 message for an intervene event (high-severity frustration / hidden tool failure)."""
    header = _t(
        language,
        en=f"🩺 Agent Doctor — caught {event.trigger}",
        zh=f"🩺 Agent Doctor — 已检测情绪/质量信号 {event.trigger}",
    )
    body = _t(
        language,
        en=(
            f"Severity: {event.severity}.\n"
            f"Evidence:\n  > {event.evidence[:400]}\n"
            f"\n{event.summary}"
        ),
        zh=(
            f"严重程度: {event.severity}\n"
            f"证据:\n  > {event.evidence[:400]}\n"
            f"\n{event.summary}"
        ),
    )
    footer = _t(
        language,
        en=f"Card: {event.card_path or 'n/a'}",
        zh=f"诊断卡: {event.card_path or '无'}",
    )
    return MessageBody(header=header, body=body, footer=footer)


def render_propose(
    *,
    proposal_id: str,
    target_kind: str,
    target_file: Path,
    patch_body: str,
    reason_summary: str,
    language: str = "en",
) -> MessageBody:
    """🩺 message proposing a patch the user can approve with a reaction."""
    header = _t(
        language,
        en=f"🩺 Agent Doctor — draft patch ({target_kind})",
        zh=f"🩺 Agent Doctor — 待批补丁 ({target_kind})",
    )
    body_en = (
        f"Reason: {reason_summary}\n"
        f"Target file: {target_file}\n"
        f"\nPatch:\n{patch_body}"
    )
    body_zh = (
        f"原因: {reason_summary}\n"
        f"目标文件: {target_file}\n"
        f"\n补丁:\n{patch_body}"
    )
    body = _t(language, en=body_en, zh=body_zh)
    footer = _t(
        language,
        en=(
            f"React ✅ to apply, ❌ to dismiss, 💬 to refine.\n"
            f"CLI fallback: agent-doctor approve {proposal_id} | "
            f"dismiss {proposal_id} | redraft {proposal_id}"
        ),
        zh=(
            f"反应 ✅ 应用，❌ 忽略，💬 重写。\n"
            f"CLI fallback: agent-doctor approve {proposal_id} | "
            f"dismiss {proposal_id} | redraft {proposal_id}"
        ),
    )
    return MessageBody(header=header, body=body, footer=footer)


def render_applied(
    *,
    proposal_id: str,
    target_file: Path,
    backup_path: Path,
    language: str = "en",
) -> MessageBody:
    """Update the original propose message after ✅ → applied."""
    header = _t(
        language,
        en=f"🩺 ✅ Applied — patch {proposal_id}",
        zh=f"🩺 ✅ 已应用 — 补丁 {proposal_id}",
    )
    body = _t(
        language,
        en=(
            f"Wrote: {target_file}\n"
            f"Backup: {backup_path}"
        ),
        zh=(
            f"已写入: {target_file}\n"
            f"备份位于: {backup_path}"
        ),
    )
    footer = _t(
        language,
        en=f"Undo: agent-doctor undo {proposal_id}",
        zh=f"撤销: agent-doctor undo {proposal_id}",
    )
    return MessageBody(header=header, body=body, footer=footer)


def render_undone(
    *,
    patch_id: str,
    target_file: Path,
    language: str = "en",
) -> MessageBody:
    """🩺 message confirming an undo restored a prior patch's target file."""
    header = _t(
        language,
        en=f"🩺 Reverted patch {patch_id}",
        zh=f"🩺 已撤销补丁 {patch_id}",
    )
    body = _t(
        language,
        en=f"Restored {target_file} from backup.",
        zh=f"已从备份恢复 {target_file}。",
    )
    return MessageBody(header=header, body=body, footer=None)


def render_digest(
    *,
    events: int,
    proposed: int,
    applied: int,
    measured_better: int,
    top_patterns: Iterable[str],
    language: str = "en",
) -> MessageBody:
    """Weekly digest summary."""
    header = _t(
        language,
        en="🩺 Agent Doctor — weekly digest",
        zh="🩺 Agent Doctor — 周报",
    )
    patterns = ", ".join(list(top_patterns)[:5]) or "none"
    body = _t(
        language,
        en=(
            f"Detected: {events}\n"
            f"Proposed: {proposed}\n"
            f"Applied: {applied}\n"
            f"Measured better: {measured_better}\n"
            f"Top patterns: {patterns}"
        ),
        zh=(
            f"检测: {events}\n"
            f"建议: {proposed}\n"
            f"已应用: {applied}\n"
            f"测得改进: {measured_better}\n"
            f"主要模式: {patterns}"
        ),
    )
    return MessageBody(header=header, body=body, footer=None)
