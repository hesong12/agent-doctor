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
