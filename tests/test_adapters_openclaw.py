"""Tests for OpenClawAdapter.

Most tests use monkeypatch on subprocess.run to avoid requiring the real
OpenClaw CLI in CI. One integration test runs only when `openclaw`
binary is on PATH (skip otherwise).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_doctor.adapters import (
    HostAdapter,
    MessageBody,
    MessageKind,
    Target,
)
from agent_doctor.adapters.openclaw import OpenClawAdapter
from agent_doctor.adapters.testing import AdapterContractTest


def _completed(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# --- detection ---------------------------------------------------------------


def test_detect_returns_none_when_openclaw_home_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", tmp_path / "missing-openclaw")
    assert OpenClawAdapter.detect() is None


def test_detect_returns_instance_when_openclaw_home_exists(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "fake-openclaw"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    instance = OpenClawAdapter.detect()
    assert isinstance(instance, OpenClawAdapter)


# --- capabilities ------------------------------------------------------------


def test_capabilities_declare_real_features(tmp_path: Path, monkeypatch) -> None:
    """When the openclaw binary is reachable, capabilities should claim
    can_send_message / can_react / can_inject_system_event / can_infer_text."""
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    fake_bin = tmp_path / "openclaw"
    fake_bin.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: str(fake_bin))

    caps = OpenClawAdapter().capabilities()

    assert caps.host_name == "openclaw"
    assert caps.can_send_message is True
    assert caps.can_react is True
    assert caps.can_list_reactions is True
    assert caps.can_inject_system_event is True
    assert caps.can_infer_text is True
    assert caps.can_infer_embedding is True


def test_capabilities_degrade_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    """Detected via ~/.openclaw but binary not on PATH — flags should still
    indicate the adapter shape but downstream calls would fail. Graceful
    degradation: every flag stays False so callers know the binary is gone."""
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)

    caps = OpenClawAdapter().capabilities()

    assert caps.host_name == "openclaw"
    assert caps.can_send_message is False
    assert caps.can_react is False
    assert caps.can_inject_system_event is False
    assert caps.can_infer_text is False


def test_capabilities_caches_channel_discovery(monkeypatch, tmp_path: Path) -> None:
    """capabilities() called twice should only call _discover_channels once."""
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    fake_bin = tmp_path / "openclaw"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: str(fake_bin))

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        return _completed(stdout='{"channels": [{"channel": "telegram"}]}')

    monkeypatch.setattr("agent_doctor.adapters.openclaw.subprocess.run", fake_run)

    adapter = OpenClawAdapter()
    adapter.capabilities()
    adapter.capabilities()
    adapter.capabilities()

    assert call_count["n"] == 1, f"_discover_channels should be called once, was {call_count['n']}"


# --- send_message ------------------------------------------------------------


def test_send_message_invokes_openclaw_message_send(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout=json.dumps({"messageId": "msg-123"}))

    monkeypatch.setattr("agent_doctor.adapters.openclaw.subprocess.run", fake_run)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    adapter = OpenClawAdapter()
    target = Target(host="openclaw", channel="telegram", recipient="@me")
    body = MessageBody(header="🩺 H", body="B")
    msg_id = adapter.send_message(target, body, MessageKind.intervene)

    assert msg_id == "msg-123"
    assert captured["cmd"][0] == "/fake/openclaw"
    assert captured["cmd"][1:5] == ["message", "send", "--channel", "telegram"]
    assert "--target" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--target") + 1] == "@me"
    assert "--message" in captured["cmd"]
    rendered_body = captured["cmd"][captured["cmd"].index("--message") + 1]
    assert "🩺" in rendered_body
    assert "--json" in captured["cmd"]


def test_send_message_falls_through_to_inbox_for_tui(monkeypatch, tmp_path: Path) -> None:
    """TUI sessions have no channel; OpenClaw adapter should fall through to
    inbox-file write (delegated via GenericAdapter logic) so we still
    deliver something the user can see."""
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")
    inbox = tmp_path / "advisory.md"
    target = Target(host="openclaw", channel="tui", recipient="local", inbox_path=inbox)
    body = MessageBody(header="🩺 TUI fallback", body="hello")

    msg_id = OpenClawAdapter().send_message(target, body, MessageKind.intervene)

    assert inbox.exists()
    assert "🩺 TUI fallback" in inbox.read_text(encoding="utf-8")
    assert msg_id  # any non-empty id


# --- list_reactions ----------------------------------------------------------


def test_list_reactions_parses_openclaw_output(monkeypatch) -> None:
    payload = {
        "reactions": [
            {"messageId": "m1", "emoji": "✅", "userId": "u1", "timestamp": 1.0},
            {"messageId": "m1", "emoji": "❌", "userId": "u2", "timestamp": 2.0},
        ]
    }

    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(stdout=json.dumps(payload)),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    target = Target(host="openclaw", channel="discord", recipient="channel:1")
    reactions = OpenClawAdapter().list_reactions(target, "m1")

    assert len(reactions) == 2
    assert reactions[0].emoji == "✅"
    assert reactions[1].emoji == "❌"


# --- inject_system_event -----------------------------------------------------


def test_inject_system_event_calls_existing_helper(monkeypatch) -> None:
    """OpenClaw adapter delegates to delivery.notify_openclaw_system_event
    so the Phase 0 fix (resolve_openclaw_binary, PATH augmentation,
    structured stderr capture) is preserved."""
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout="ok\n")

    monkeypatch.setattr("agent_doctor.adapters.openclaw.subprocess.run", fake_run)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    OpenClawAdapter().inject_system_event("HEY", mode="now")

    assert captured["cmd"][1:3] == ["system", "event"]
    assert "--mode" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--mode") + 1] == "now"
    assert "--text" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--text") + 1] == "HEY"


# --- infer_text --------------------------------------------------------------


def test_infer_text_uses_openclaw_infer_model_run(monkeypatch) -> None:
    payload = {"outputs": [{"text": "classification: high"}]}
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(stdout=json.dumps(payload)),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    text = OpenClawAdapter().infer_text("classify this", model="claude-haiku")

    assert text == "classification: high"


def test_infer_text_with_default_model_omits_model_flag(monkeypatch) -> None:
    captured: dict = {}
    payload = {"outputs": [{"text": "ok"}]}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout=json.dumps(payload))

    monkeypatch.setattr("agent_doctor.adapters.openclaw.subprocess.run", fake_run)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    OpenClawAdapter().infer_text("hi")

    assert "--model" not in captured["cmd"]


# --- non-zero rc / stderr branches ------------------------------------------
# Lock in the structured-error contract: every method that shells out to
# openclaw must raise RuntimeError with rc + stderr in the message when the
# subprocess fails. (add_reaction / list_reactions are intentionally
# best-effort and tested separately under "silent failure logging".)


def test_send_message_raises_on_nonzero_rc(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(rc=1, stderr="boom"),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    target = Target(host="openclaw", channel="telegram", recipient="@me")
    body = MessageBody(header="🩺 H", body="B")
    with pytest.raises(RuntimeError, match=r"rc=1.*'boom'"):
        OpenClawAdapter().send_message(target, body, MessageKind.intervene)


def test_edit_message_raises_on_nonzero_rc(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(rc=1, stderr="boom"),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    target = Target(host="openclaw", channel="telegram", recipient="@me")
    body = MessageBody(header="🩺 H", body="B")
    with pytest.raises(RuntimeError, match=r"rc=1.*'boom'"):
        OpenClawAdapter().edit_message(target, "msg-1", body)


def test_inject_system_event_raises_on_nonzero_rc(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(rc=2, stderr="bad"),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    with pytest.raises(RuntimeError, match=r"rc=2.*'bad'"):
        OpenClawAdapter().inject_system_event("hi")


def test_infer_text_raises_on_nonzero_rc(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(rc=1, stderr="api error"),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    with pytest.raises(RuntimeError, match=r"rc=1.*'api error'"):
        OpenClawAdapter().infer_text("hi")


def test_infer_embedding_raises_on_nonzero_rc(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.subprocess.run",
        lambda cmd, **kw: _completed(rc=1, stderr="no model"),
    )
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: "/fake/openclaw")

    with pytest.raises(RuntimeError, match=r"rc=1.*'no model'"):
        OpenClawAdapter().infer_embedding("hi")


# --- session_metadata: TUI detection ---------------------------------------


def test_session_metadata_recognizes_tui_session_key(tmp_path: Path) -> None:
    """sessionKey 'agent:main:tui-XXXX' should be classified as channel='tui'."""
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("", encoding="utf-8")
    trajectory = tmp_path / "session.trajectory.jsonl"
    trajectory.write_text(
        json.dumps({"sessionKey": "agent:main:tui-abc123", "sessionId": "sess-1"}) + "\n",
        encoding="utf-8",
    )

    meta = OpenClawAdapter().session_metadata(jsonl)
    assert meta.channel == "tui"


def test_session_metadata_does_not_match_intuit_substring(tmp_path: Path) -> None:
    """A hypothetical 'intuit-bot' channel must NOT be misread as TUI just
    because 'tui' appears as a substring of 'intuit'."""
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("", encoding="utf-8")
    trajectory = tmp_path / "session.trajectory.jsonl"
    trajectory.write_text(
        json.dumps({"sessionKey": "agent:main:intuit-bot-channel", "sessionId": "sess-2"}) + "\n",
        encoding="utf-8",
    )

    meta = OpenClawAdapter().session_metadata(jsonl)
    assert meta.channel == "channel"


# --- contract conformance ---------------------------------------------------


class TestOpenClawAdapterContract(AdapterContractTest):
    """OpenClawAdapter must satisfy the contract; skip if openclaw absent."""

    ADAPTER = OpenClawAdapter

    @pytest.fixture()
    def adapter(self, tmp_path, monkeypatch):
        # Provide a deterministic detection environment for contract tests
        home = tmp_path / "openclaw-home"
        home.mkdir()
        monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
        monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
        # binary missing → capabilities all False → NotImplementedError on call
        instance = OpenClawAdapter.detect()
        if instance is None:
            pytest.skip("OpenClawAdapter.detect() returned None")
        return instance


# --- integration (skip when binary missing) ----------------------------------


@pytest.mark.skipif(not shutil.which("openclaw"), reason="openclaw not on PATH")
def test_real_openclaw_infer_text_smoke() -> None:
    """If the real CLI is present, do one tiny inference to prove the wiring.

    If the host's openclaw provider is misconfigured (no API key, missing
    model, etc.), skip rather than fail — the failure is a host-config
    issue, not a wiring bug. Reaching the CLI proves the wiring. Only
    skip on a known set of host-config error patterns; let real wiring
    bugs surface as failures.
    """
    adapter = OpenClawAdapter()
    if not adapter.capabilities().can_infer_text:
        pytest.skip("OpenClaw capabilities don't include text inference here")

    HOST_CONFIG_HINTS = (
        "no api key",
        "model not found",
        "not configured",
        "unauthorized",
        "no text output returned",
        "model is not available",
    )
    try:
        out = adapter.infer_text("Reply with the single word: ok")
    except RuntimeError as exc:
        msg = str(exc).lower()
        if any(needle in msg for needle in HOST_CONFIG_HINTS):
            pytest.skip(f"OpenClaw inference unavailable on this host: {exc}")
        raise  # real wiring bug
    assert "ok" in out.lower() or "OK" in out
