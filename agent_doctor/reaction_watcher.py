"""Reaction watcher: poll for ✅/❌/💬 on pending proposals.

The first non-neutral reaction within 5min of detection wins. Later
reactions log but do not reverse the decision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .adapters import HostAdapter, Reaction, Target
from .proposer import Proposal, ProposalState

REACTION_APPLY = "✅"
REACTION_DISMISS = "❌"
REACTION_REFINE = "💬"


@dataclass(frozen=True)
class StateTransition:
    proposal_id: str
    old_state: ProposalState
    new_state: ProposalState
    reason: str  # reaction emoji or "expired" or "conflict"


def poll_pending_proposals(
    proposals: Iterable[Proposal],
    *,
    adapter: HostAdapter,
    applier: Callable[[Proposal], bool],
) -> list[StateTransition]:
    """Poll once: for each pending proposal, list its reactions and act.

    applier is the function that actually writes the patch to the live
    config. Returns True on success → state=applied; False → still
    pending. Tasks 6+8 wire the real applier; tests pass a stub.
    """
    transitions: list[StateTransition] = []
    now = time.time()

    for proposal in proposals:
        if proposal.state != "pending":
            continue
        if proposal.ttl_at <= now:
            transitions.append(StateTransition(
                proposal_id=proposal.id,
                old_state=proposal.state,
                new_state="expired",
                reason="ttl_expired",
            ))
            continue
        if not proposal.message_id:
            continue  # not yet posted; skip until speaker has posted it
        target = _target_from_proposal(proposal)
        if target is None:
            continue
        try:
            reactions = adapter.list_reactions(target, proposal.message_id)
        except Exception:
            continue  # transient; try next poll
        decision = _decide_from_reactions(reactions)
        if decision is None:
            continue
        if decision == REACTION_APPLY:
            ok = applier(proposal)
            if ok:
                transitions.append(StateTransition(
                    proposal_id=proposal.id,
                    old_state=proposal.state,
                    new_state="applied",
                    reason=REACTION_APPLY,
                ))
            # If applier returns False, no transition — proposal stays pending
            # so the next poll can retry (e.g., transient lock release).
        elif decision == REACTION_DISMISS:
            transitions.append(StateTransition(
                proposal_id=proposal.id,
                old_state=proposal.state,
                new_state="dismissed",
                reason=REACTION_DISMISS,
            ))
        elif decision == REACTION_REFINE:
            transitions.append(StateTransition(
                proposal_id=proposal.id,
                old_state=proposal.state,
                new_state="refining",
                reason=REACTION_REFINE,
            ))
    return transitions


def _decide_from_reactions(reactions: Iterable[Reaction]) -> str | None:
    """First non-neutral reaction wins. Return None if none seen."""
    sorted_reactions = sorted(reactions, key=lambda r: r.at)
    for r in sorted_reactions:
        if r.emoji in (REACTION_APPLY, REACTION_DISMISS, REACTION_REFINE):
            return r.emoji
    return None


def _target_from_proposal(proposal: Proposal) -> Target | None:
    if not (proposal.target_host and proposal.target_channel):
        return None
    return Target(
        host=proposal.target_host,
        channel=proposal.target_channel,
        recipient=proposal.target_recipient or "",
    )
