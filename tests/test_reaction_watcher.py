"""Tests for reaction_watcher."""
import time
from pathlib import Path

import pytest

from agent_doctor.adapters import Reaction, Target
from agent_doctor.proposer import Proposal
from agent_doctor.reaction_watcher import poll_pending_proposals


def _proposal(state: str = "pending") -> Proposal:
    return Proposal(
        id="p-1",
        session_id="s-1",
        finding_id="f-1",
        target_kind="memory",
        target_file_hint="/tmp/MEMORY.md",
        patch_body="- entry",
        reason_summary="x",
        baseline_hash=None,
        state=state,
        message_id="msg-1",
        target_host="generic",
        target_channel="inbox",
        target_recipient="",
        created_at=time.time(),
        ttl_at=time.time() + 3600,
    )


class _ReactingAdapter:
    """Test adapter that returns canned reactions."""
    def __init__(self, reactions: list[Reaction]) -> None:
        self.reactions = reactions

    def list_reactions(self, target, message_id):
        return list(self.reactions)


def test_poll_marks_proposal_applied_on_check_reaction() -> None:
    proposals = [_proposal()]
    reactions = [Reaction(message_id="msg-1", emoji="✅", user_id="u", at=time.time())]

    transitions = poll_pending_proposals(
        proposals,
        adapter=_ReactingAdapter(reactions),
        applier=lambda p: True,  # pretend apply succeeded
    )
    assert any(t.new_state == "applied" for t in transitions)


def test_poll_marks_proposal_dismissed_on_x_reaction() -> None:
    proposals = [_proposal()]
    reactions = [Reaction(message_id="msg-1", emoji="❌", user_id="u", at=time.time())]

    transitions = poll_pending_proposals(
        proposals,
        adapter=_ReactingAdapter(reactions),
        applier=lambda p: True,
    )
    assert any(t.new_state == "dismissed" for t in transitions)


def test_poll_marks_proposal_refining_on_speech_bubble() -> None:
    proposals = [_proposal()]
    reactions = [Reaction(message_id="msg-1", emoji="💬", user_id="u", at=time.time())]

    transitions = poll_pending_proposals(
        proposals,
        adapter=_ReactingAdapter(reactions),
        applier=lambda p: True,
    )
    assert any(t.new_state == "refining" for t in transitions)


def test_poll_skips_already_resolved_proposals() -> None:
    proposals = [_proposal(state="applied"), _proposal(state="dismissed")]
    transitions = poll_pending_proposals(
        proposals,
        adapter=_ReactingAdapter([]),  # no reactions
        applier=lambda p: True,
    )
    assert transitions == []


def test_poll_expires_proposals_past_ttl() -> None:
    p = _proposal()
    expired = Proposal(**{**p.to_dict(), "ttl_at": time.time() - 1})
    transitions = poll_pending_proposals(
        [expired],
        adapter=_ReactingAdapter([]),
        applier=lambda p: True,
    )
    assert any(t.new_state == "expired" for t in transitions)


def test_poll_first_non_neutral_reaction_wins() -> None:
    """If both ❌ and ✅ are present, the FIRST one (by timestamp) wins."""
    now = time.time()
    proposals = [_proposal()]
    reactions = [
        Reaction(message_id="msg-1", emoji="❌", user_id="u1", at=now),  # first
        Reaction(message_id="msg-1", emoji="✅", user_id="u2", at=now + 1),  # second
    ]
    transitions = poll_pending_proposals(
        proposals,
        adapter=_ReactingAdapter(reactions),
        applier=lambda p: True,
    )
    # ❌ came first → dismissed
    assert any(t.new_state == "dismissed" for t in transitions)
    # ✅ should NOT also fire
    assert not any(t.new_state == "applied" for t in transitions)


def test_poll_ignores_proposals_without_message_id() -> None:
    """A proposal that hasn't been posted yet (message_id None) is skipped."""
    p = _proposal()
    not_posted = Proposal(**{**p.to_dict(), "message_id": None})
    transitions = poll_pending_proposals(
        [not_posted],
        adapter=_ReactingAdapter([Reaction(message_id="any", emoji="✅", user_id="u", at=time.time())]),
        applier=lambda p: True,
    )
    assert transitions == []  # nothing to poll on


def test_poll_does_not_apply_when_applier_returns_false() -> None:
    """If applier(p) returns False, the proposal stays pending (no state transition)."""
    proposals = [_proposal()]
    reactions = [Reaction(message_id="msg-1", emoji="✅", user_id="u", at=time.time())]
    transitions = poll_pending_proposals(
        proposals,
        adapter=_ReactingAdapter(reactions),
        applier=lambda p: False,  # apply failed (e.g., conflict)
    )
    # No transition recorded since apply failed
    assert all(t.new_state != "applied" for t in transitions)
