"""Tests for the proposer."""
import json
import time
from pathlib import Path

import pytest

from agent_doctor.proposer import (
    Proposal,
    draft_proposals_for_session,
    load_proposals,
    save_proposals,
)
from agent_doctor.schema import Evidence, Finding


def _frust_finding(session: str = "s1", count: int = 3) -> Finding:
    return Finding(
        id="uf-001",
        failure_mode="user_frustration_signal",
        session_id=session,
        severity="high",
        title="User frustration",
        diagnosis="repeated frustration",
        count=count,
        confidence=0.9,
        evidence=[
            Evidence(
                file="x.jsonl",
                line=1,
                role="user",
                quote="你太蠢了",
            )
        ],
        recommendations=[],
        eval_case={},
    )


def test_draft_proposals_skips_below_threshold(tmp_path: Path) -> None:
    """count < threshold → no proposal."""
    finding = _frust_finding(count=1)  # below default threshold of 3
    proposals = draft_proposals_for_session(
        findings=[finding], session_id="s1", min_count=3,
    )
    assert proposals == []


def test_draft_proposals_above_threshold_yields_proposal(tmp_path: Path) -> None:
    finding = _frust_finding(count=3)
    proposals = draft_proposals_for_session(
        findings=[finding], session_id="s1", min_count=3,
    )
    assert len(proposals) >= 1
    p = proposals[0]
    assert p.session_id == "s1"
    assert p.target_kind in ("memory", "identity", "sop", "tool_discipline")  # eval is filtered
    assert p.patch_body  # non-empty
    assert p.state == "pending"
    assert p.id  # non-empty UUID-like


def test_draft_proposals_skips_eval_target_kind() -> None:
    """Eval recommendations don't need user approval; they auto-stage elsewhere."""
    # user_frustration_signal recommends [identity, sop, eval]; eval should be filtered.
    finding = _frust_finding(count=3)
    proposals = draft_proposals_for_session(
        findings=[finding], session_id="s1", min_count=3,
    )
    assert all(p.target_kind != "eval" for p in proposals)


def test_proposer_caps_at_max_per_session() -> None:
    """At most max_per_session proposals; one per failure_mode."""
    findings = []
    for i, mode in enumerate(["user_frustration_signal", "memory_failure", "verification_failure", "execution_discipline"]):
        f = _frust_finding(count=5)
        f = Finding(
            id=f"uf-{i}",
            failure_mode=mode,
            session_id="s1",
            severity="high",
            title=f.title,
            diagnosis=f.diagnosis,
            count=5,
            confidence=f.confidence,
            evidence=f.evidence,
            recommendations=[],
            eval_case={},
        )
        findings.append(f)
    proposals = draft_proposals_for_session(
        findings=findings, session_id="s1", min_count=3, max_per_session=3,
    )
    assert len(proposals) <= 3
    # Should not have duplicate failure modes
    modes = [p.finding_id for p in proposals]
    assert len(modes) == len(set(modes))


def test_save_load_proposals_roundtrip(tmp_path: Path) -> None:
    finding = _frust_finding(count=3)
    proposals = draft_proposals_for_session(
        findings=[finding], session_id="s1", min_count=3,
    )
    out = tmp_path / "proposals.jsonl"
    save_proposals(out, proposals)

    loaded = load_proposals(out)
    assert len(loaded) == len(proposals)
    assert loaded[0].id == proposals[0].id
    assert loaded[0].state == "pending"


def test_save_proposals_writes_0o600(tmp_path: Path) -> None:
    finding = _frust_finding(count=3)
    proposals = draft_proposals_for_session(findings=[finding], session_id="s1", min_count=3)
    out = tmp_path / "proposals.jsonl"
    save_proposals(out, proposals)

    import stat
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
