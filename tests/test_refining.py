"""Tests for refining loop."""
import json
import time
from pathlib import Path

import pytest

from agent_doctor.proposer import Proposal
from agent_doctor.refining import (
    find_followup_message,
    find_refining_proposals,
    redraft_pending,
    redraft_with_followup,
)
from agent_doctor.schema import Message


def _proposal(state: str = "refining", body: str = "- original entry") -> Proposal:
    return Proposal(
        id="p-1",
        session_id="s-1",
        finding_id="f-1",
        target_kind="memory",
        target_file_hint="",
        patch_body=body,
        reason_summary="x",
        baseline_hash=None,
        state=state,
        message_id="msg-1",
        target_host="generic",
        target_channel="inbox",
        target_recipient="",
        created_at=time.time() - 100,
        ttl_at=time.time() + 3600,
    )


class _FakeAdapter:
    def __init__(self, response=None, can_infer=True):
        self.response = response
        self.can_infer = can_infer

    def capabilities(self):
        from agent_doctor.adapters import HostCapabilities
        return HostCapabilities(host_name="fake", detected_at=Path("/"), can_infer_text=self.can_infer)

    def infer_text(self, prompt, *, model=None):
        if self.response is None:
            raise RuntimeError("fail")
        return self.response


def test_find_refining_filters_by_state() -> None:
    a = _proposal(state="refining")
    b = _proposal(state="pending")
    c = _proposal(state="applied")
    found = find_refining_proposals([a, b, c])
    assert found == [a]


def test_redraft_with_followup_no_adapter_appends_note() -> None:
    proposal = _proposal(body="- old entry")
    new = redraft_with_followup(proposal, "actually I prefer brief output", adapter=None)
    assert new.state == "pending"
    assert "old entry" in new.patch_body
    assert "brief output" in new.patch_body  # user follow-up appended
    assert new.id == proposal.id  # same id, just redrafted body


def test_redraft_with_followup_uses_llm_when_available() -> None:
    proposal = _proposal(body="- entry")
    canned = json.dumps({"new_body": "- entry (refined: brief)", "rationale": "user wants brevity"})
    adapter = _FakeAdapter(response=canned)
    new = redraft_with_followup(proposal, "make it briefer", adapter=adapter)
    assert "brief" in new.patch_body
    assert new.state == "pending"


def test_redraft_falls_back_when_llm_returns_malformed() -> None:
    proposal = _proposal(body="- entry")
    adapter = _FakeAdapter(response="not json")
    new = redraft_with_followup(proposal, "make it briefer", adapter=adapter)
    # Falls back to append-mode
    assert "make it briefer" in new.patch_body
    assert new.state == "pending"


def test_find_followup_message_returns_last_user_in_session() -> None:
    messages = [
        Message(file="x", line=1, session_id="s-1", role="user", content="first"),
        Message(file="x", line=2, session_id="s-1", role="assistant", content="reply"),
        Message(file="x", line=3, session_id="s-1", role="user", content="follow up"),
        Message(file="x", line=4, session_id="s-2", role="user", content="other session"),
    ]
    found = find_followup_message(messages, "s-1", after_ts=0)
    assert found is not None
    assert found.content == "follow up"


def test_find_followup_returns_none_when_no_user_messages_in_session() -> None:
    messages = [
        Message(file="x", line=1, session_id="s-other", role="user", content="x"),
    ]
    assert find_followup_message(messages, "s-1", after_ts=0) is None


def test_redraft_pending_processes_all_refining() -> None:
    proposals = [
        _proposal(state="pending"),
        _proposal(state="refining"),
        _proposal(state="dismissed"),
    ]
    proposals[1] = Proposal(**{**proposals[1].to_dict(), "id": "p-refining"})

    messages = [
        Message(file="x", line=1, session_id="s-1", role="user", content="please make it short"),
    ]

    redrafts = redraft_pending(proposals, messages, adapter=None)
    assert len(redrafts) == 1
    assert redrafts[0].id == "p-refining"
    assert redrafts[0].state == "pending"
    assert "make it short" in redrafts[0].patch_body
