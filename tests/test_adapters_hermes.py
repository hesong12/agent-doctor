"""Tests for HermesAdapter (stub).

Hermes outbound surface (message send, reactions, inference) is TBD;
detect() should return an instance when ~/.hermes exists and
capabilities should declare honestly: JSONL ingest yes, channel
delivery no.
"""
from pathlib import Path

import pytest

from agent_doctor.adapters import HostAdapter, MessageBody, MessageKind, Target
from agent_doctor.adapters.hermes import HermesAdapter
from agent_doctor.adapters.testing import AdapterContractTest


def test_hermes_detect_returns_none_when_home_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", tmp_path / "missing-hermes")
    assert HermesAdapter.detect() is None


def test_hermes_detect_returns_instance_when_home_exists(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "fake-hermes"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)
    assert isinstance(HermesAdapter.detect(), HermesAdapter)


def test_hermes_capabilities_are_partial(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)
    caps = HermesAdapter().capabilities()

    assert caps.host_name == "hermes"
    assert caps.detected_at == home
    assert caps.skill_dir is not None
    # Outbound surface unknown / not implemented yet
    assert caps.can_send_message is False
    assert caps.can_write_inbox is True
    assert caps.can_react is False
    assert caps.can_inject_system_event is False
    assert caps.can_infer_text is False


def test_hermes_send_message_falls_through_to_inbox(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)
    inbox = tmp_path / "inbox.md"
    target = Target(host="hermes", channel="inbox", recipient="", inbox_path=inbox)
    body = MessageBody(header="🩺 hermes", body="hi")

    msg_id = HermesAdapter().send_message(target, body, MessageKind.intervene)

    assert inbox.exists()
    assert "hi" in inbox.read_text(encoding="utf-8")
    assert msg_id


def test_hermes_infer_text_raises(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)
    with pytest.raises(NotImplementedError):
        HermesAdapter().infer_text("ping")


# --- contract conformance ---------------------------------------------------


class TestHermesAdapterContract(AdapterContractTest):
    ADAPTER = HermesAdapter

    @pytest.fixture()
    def adapter(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)
        instance = HermesAdapter.detect()
        assert instance is not None
        return instance


def test_hermes_install_skill_writes_to_skill_dir(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)

    written = HermesAdapter().install_skill("# hermes skill")

    assert written == home / "skills" / "autonomous-ai-agents" / "agent-doctor" / "SKILL.md"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "# hermes skill"
