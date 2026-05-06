"""Proposer: drafts patches at session-end or N-detection threshold.

Each proposal is one of: memory, identity, sop, tool_discipline.
Eval-target recommendations are filtered out (they auto-stage; no
user approval needed).

Memory and tool_discipline are append-only (no conflict). Identity
and SOP record a baseline file hash so the applier can detect
concurrent edits and refuse to overwrite. (Baseline hash is set at
apply time, not draft time, so it reflects the file state immediately
before the write.)
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

from .recommend import build_recommendations
from .schema import Finding

ProposalState = Literal["pending", "applied", "dismissed", "refining", "expired", "conflict"]


@dataclass(frozen=True)
class Proposal:
    id: str
    session_id: str
    finding_id: str
    target_kind: str  # memory / identity / sop / tool_discipline
    target_file_hint: str  # adapter-resolved path is filled in at apply time
    patch_body: str
    reason_summary: str
    baseline_hash: str | None  # set at apply time for edit-style patches
    state: ProposalState
    message_id: str | None  # set after speaker posts the propose message
    target_host: str | None
    target_channel: str | None
    target_recipient: str | None
    created_at: float
    ttl_at: float
    resolved_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def draft_proposals_for_session(
    *,
    findings: Iterable[Finding],
    session_id: str,
    min_count: int = 3,
    max_per_session: int = 3,
    ttl_hours: float = 24.0,
) -> list[Proposal]:
    """Draft proposals for the session.

    Filters: only findings with count >= min_count AND severity high
    qualify. Caps at max_per_session, one per failure_mode.
    """
    qualified = [
        f for f in findings
        if f.session_id == session_id
        and f.count >= min_count
        and f.severity == "high"
    ]
    qualified.sort(key=lambda f: f.count, reverse=True)

    out: list[Proposal] = []
    seen_modes: set[str] = set()
    for finding in qualified:
        if len(out) >= max_per_session:
            break
        if finding.failure_mode in seen_modes:
            continue
        seen_modes.add(finding.failure_mode)
        recs = build_recommendations(
            finding.failure_mode,
            finding.evidence,
            finding.count,
        )
        for rec in recs:
            target_kind = rec.get("target", "")
            if target_kind == "eval":
                continue  # auto-staged elsewhere
            if target_kind not in ("memory", "identity", "sop", "tool_discipline"):
                continue
            proposal = _build_proposal(
                finding=finding,
                target_kind=target_kind,
                patch_body=rec.get("proposal", ""),
                ttl_hours=ttl_hours,
            )
            out.append(proposal)
            break  # one proposal per finding (its first non-eval target)
    return out


def _build_proposal(
    *,
    finding: Finding,
    target_kind: str,
    patch_body: str,
    ttl_hours: float,
) -> Proposal:
    now = time.time()
    proposal_id = uuid.uuid4().hex[:12]
    summary = f"{finding.failure_mode} fired {finding.count}x in this session"
    return Proposal(
        id=proposal_id,
        session_id=finding.session_id,
        finding_id=finding.id,
        target_kind=target_kind,
        target_file_hint="",  # adapter resolves at apply time via capabilities()
        patch_body=patch_body,
        reason_summary=summary,
        baseline_hash=None,  # populated at apply time for edit-style kinds
        state="pending",
        message_id=None,
        target_host=None,
        target_channel=None,
        target_recipient=None,
        created_at=now,
        ttl_at=now + ttl_hours * 3600,
    )


def save_proposals(path: Path, proposals: Iterable[Proposal]) -> None:
    """Append proposals to JSONL, 0o600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as h:
            for p in proposals:
                h.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
    finally:
        os.chmod(path, 0o600)


def load_proposals(path: Path) -> list[Proposal]:
    """Read all proposals (any state)."""
    if not path.exists():
        return []
    out: list[Proposal] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(Proposal(**d))
        except (json.JSONDecodeError, TypeError):
            continue
    return out
