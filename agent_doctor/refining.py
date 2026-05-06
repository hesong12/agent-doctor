"""Refining loop: redraft a refining proposal using a follow-up user message.

When the user reacts 💬 on a propose message, the proposal transitions
to state=refining. The next time the user sends a message in the same
session, this module:
  - Picks it up as additional context
  - Redrafts the proposal's patch_body to incorporate the user's
    refinement (via host LLM if available, else simple rule-based merge)
  - Resets state=pending with a new ttl
  - Returns the new Proposal records

The autopilot loop calls redraft_pending each cycle.
"""
from __future__ import annotations

import time
from dataclasses import replace as _dc_replace
from typing import Iterable, Optional

from .proposer import Proposal
from .schema import Message


_REDRAFT_PROMPT = """A user previously refined a draft patch with a follow-up message.

Original patch body:
{original_body}

Original reason: {reason_summary}

User's follow-up message (their refinement input):
{follow_up}

Produce a new version of the patch body that incorporates the user's input. Reply with strict JSON only: {{"new_body": "...", "rationale": "..."}}."""


def find_refining_proposals(
    proposals: Iterable[Proposal],
) -> list[Proposal]:
    return [p for p in proposals if p.state == "refining"]


def redraft_with_followup(
    proposal: Proposal,
    follow_up_message: str,
    *,
    adapter=None,
) -> Proposal:
    """Return a new Proposal with redrafted patch_body and state=pending.

    If adapter has can_infer_text, asks the host LLM to integrate the
    user's follow-up into the patch. Otherwise falls back to a simple
    "append user note" merge.
    """
    new_body = proposal.patch_body
    rationale_extra = ""
    if adapter is not None:
        try:
            caps = adapter.capabilities()
            if caps.can_infer_text:
                prompt = _REDRAFT_PROMPT.format(
                    original_body=proposal.patch_body,
                    reason_summary=proposal.reason_summary,
                    follow_up=follow_up_message[:1000],
                )
                response = adapter.infer_text(prompt)
                parsed = _parse_redraft_response(response)
                if parsed is not None:
                    new_body = parsed["new_body"] or proposal.patch_body
                    rationale_extra = parsed.get("rationale", "")
        except Exception:
            pass  # graceful: keep original body if LLM call fails

    if new_body == proposal.patch_body:
        # Fallback: append the user's note as a comment
        new_body = (
            proposal.patch_body.rstrip("\n")
            + f"\n# (refined per user follow-up: {follow_up_message[:200]})"
        )

    now = time.time()
    return _dc_replace(
        proposal,
        patch_body=new_body,
        reason_summary=(proposal.reason_summary + f" | refined: {rationale_extra}").strip(" |"),
        state="pending",  # back to pending after redraft
        message_id=None,  # speaker reposts -> new message_id
        baseline_hash=None,  # reset; applier will pick fresh
        created_at=now,
        ttl_at=now + 24 * 3600,
        resolved_at=None,
    )


def find_followup_message(
    messages: list[Message],
    session_id: str,
    after_ts: float,
) -> Optional[Message]:
    """Find the first user message in `messages` that's:
    - in `session_id`
    - has line/file ordering AFTER the proposal was created (best-effort)
    For simplicity, returns the LAST user message in the session that's
    new since the proposal — the autopilot's --changed-only mode means
    we only see fresh content per cycle.
    """
    candidates = [
        m for m in messages
        if m.role == "user" and m.session_id == session_id
    ]
    return candidates[-1] if candidates else None


def redraft_pending(
    proposals: list[Proposal],
    messages: list[Message],
    *,
    adapter=None,
) -> list[Proposal]:
    """For each refining proposal, find a follow-up user message and redraft.

    Returns the list of NEW proposals (one per successful redraft). Caller
    is responsible for replacing the old proposals with the new ones in
    proposals.jsonl.
    """
    redrafts: list[Proposal] = []
    refining = find_refining_proposals(proposals)
    if not refining:
        return redrafts

    for proposal in refining:
        followup = find_followup_message(
            messages, proposal.session_id, proposal.resolved_at or proposal.created_at
        )
        if followup is None:
            continue
        new_proposal = redraft_with_followup(
            proposal, followup.content, adapter=adapter,
        )
        redrafts.append(new_proposal)
    return redrafts


def _parse_redraft_response(text: str) -> dict | None:
    import json
    try:
        d = json.loads(text.strip())
    except json.JSONDecodeError:
        s = text.find("{")
        e = text.rfind("}")
        if s >= 0 and e > s:
            try:
                d = json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(d, dict) or "new_body" not in d:
        return None
    return {
        "new_body": str(d["new_body"]),
        "rationale": str(d.get("rationale", "")),
    }
