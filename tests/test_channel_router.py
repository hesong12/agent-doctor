"""Tests for channel_router.resolve()."""
from pathlib import Path

import pytest

from agent_doctor.adapters import GenericAdapter, OpenClawAdapter, Target
from agent_doctor.channel_router import resolve


def test_resolve_returns_target_for_jsonl_via_openclaw_adapter(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)

    # Simulate an OpenClaw TUI session — trajectory file with sessionKey
    sessions = home / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    jsonl = sessions / "trace-abc.jsonl"
    jsonl.write_text(
        '{"session_id": "trace-abc", "role": "user", "content": "hello"}\n',
        encoding="utf-8",
    )
    trajectory = sessions / "trace-abc.trajectory.jsonl"
    trajectory.write_text(
        '{"sessionKey": "agent:main:tui-bf5aecdf", "sessionId": "trace-abc"}\n',
        encoding="utf-8",
    )

    adapter = OpenClawAdapter()
    target, language = resolve(jsonl, adapter)

    assert isinstance(target, Target)
    assert target.host == "openclaw"
    assert target.channel == "tui"
    # Inbox path should be set as fallback for TUI
    assert target.inbox_path is not None


def test_resolve_with_generic_adapter_yields_inbox_target(tmp_path: Path) -> None:
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        '{"session_id": "session", "role": "user", "content": "hi"}\n',
        encoding="utf-8",
    )

    target, language = resolve(jsonl, GenericAdapter())

    assert target.host == "generic"
    assert target.inbox_path is not None
    assert language in ("en", "zh")


def test_resolve_falls_back_to_tui_when_channel_not_in_capabilities(tmp_path: Path, monkeypatch) -> None:
    """If session_metadata returns a channel that is not in caps.available_channels,
    route to TUI/inbox so we don't try to send to a non-existent host channel.

    Regression for: OpenClawAdapter sending --channel=channel/--channel=generic
    because the session was misclassified and the host had no such channel.
    """
    from agent_doctor.adapters import HostCapabilities, SessionMetadata

    class FakeAdapter:
        def capabilities(self) -> HostCapabilities:
            return HostCapabilities(
                host_name="openclaw",
                detected_at=tmp_path,
                can_send_message=True,
                can_edit_message=True,
                can_react=True,
                can_list_reactions=True,
                can_inject_system_event=True,
                can_infer_text=False,
                can_infer_embedding=False,
                default_inference_model=None,
                available_models=(),
                available_channels=("telegram",),  # only telegram is real
                skill_dir=None,
                memory_writable=None,
                identity_writable=None,
                sop_writable=None,
            )

        def session_metadata(self, p: Path) -> SessionMetadata:
            # Return an unknown channel — this is what the broken adapter did
            return SessionMetadata(
                session_id="abc",
                language="zh",
                channel="channel",  # placeholder string, not real
                recipient="agent:main:main",
            )

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")

    target, _ = resolve(jsonl, FakeAdapter())

    # Must NOT pass the bogus channel through; should fall back to TUI/inbox
    assert target.channel == "tui"
    assert target.inbox_path is not None
