"""Tests for speaker.render_* templates."""
from pathlib import Path

import pytest

from agent_doctor.adapters import MessageBody
from agent_doctor.autopilot import AutopilotEvent
from agent_doctor.speaker import (
    render_applied,
    render_digest,
    render_intervene,
    render_propose,
    render_undone,
)


def _event(trigger: str = "user_frustration_signal", language: str = "en") -> AutopilotEvent:
    return AutopilotEvent(
        id="evt-1",
        platform="openclaw",
        action="intervene",
        trigger=trigger,
        severity="high",
        session_id="sess-1",
        message_file="/tmp/sess-1.jsonl",
        message_line=2,
        summary="user is frustrated",
        evidence="你太蠢了",
        finding_ids=["uf-1"],
    )


def test_render_intervene_includes_trigger_and_evidence_en() -> None:
    body = render_intervene(_event(), language="en")

    assert isinstance(body, MessageBody)
    assert "🩺" in body.header
    assert "user_frustration_signal" in body.header.lower() or "frustration" in body.body.lower()
    assert "你太蠢了" in body.body
    assert body.footer  # always has a footer (card path or CLI hint)


def test_render_intervene_localizes_to_chinese() -> None:
    body = render_intervene(_event(language="zh"), language="zh")

    rendered = body.render()
    # Chinese wrapper words appear; English chrome is gone
    assert any(ch in rendered for ch in ("已检测", "情绪", "干预"))


def test_render_propose_includes_patch_body_and_reaction_hint() -> None:
    body = render_propose(
        proposal_id="p-1",
        target_kind="memory",
        target_file=Path("/Users/x/.openclaw/memory/MEMORY.md"),
        patch_body="- User dislikes verbose terminal output.",
        reason_summary="3x repeated correction in session",
        language="en",
    )
    rendered = body.render()
    assert "memory" in rendered.lower()
    assert "User dislikes verbose" in rendered
    # Reaction hints visible
    assert "✅" in rendered
    assert "❌" in rendered
    # CLI fallback for hosts without reactions
    assert "agent-doctor approve p-1" in rendered or "approve p-1" in rendered


def test_render_applied_marks_patch_applied_with_undo_hint() -> None:
    body = render_applied(
        proposal_id="p-1",
        target_file=Path("/Users/x/.openclaw/memory/MEMORY.md"),
        backup_path=Path("/Users/x/.agent-doctor/backups/p-1/MEMORY.md.bak"),
        language="en",
    )
    rendered = body.render()
    assert "applied" in rendered.lower() or "✅" in rendered
    assert "agent-doctor undo p-1" in rendered


def test_render_undone_explains_restoration() -> None:
    body = render_undone(patch_id="p-1", target_file=Path("/x/MEMORY.md"), language="en")
    rendered = body.render()
    assert "reverted" in rendered.lower() or "restored" in rendered.lower()
    assert "p-1" in rendered


def test_render_digest_summarizes_week() -> None:
    body = render_digest(
        events=12,
        proposed=7,
        applied=5,
        measured_better=4,
        top_patterns=["memory_failure", "verification_failure"],
        language="en",
    )
    rendered = body.render()
    assert "12" in rendered  # detection count
    assert "5" in rendered  # apply count
    assert "memory_failure" in rendered
    assert "🩺" in rendered
