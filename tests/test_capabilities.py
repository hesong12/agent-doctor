"""Tests for capability detection."""
from pathlib import Path

from agent_doctor.adapters import GenericAdapter, HermesAdapter, OpenClawAdapter
from agent_doctor.capabilities import detect_hosts


def test_detect_hosts_includes_generic_when_nothing_else_present(tmp_path: Path, monkeypatch) -> None:
    """No ~/.openclaw, no ~/.hermes → only GenericAdapter."""
    monkeypatch.setattr(
        "agent_doctor.adapters.openclaw.OPENCLAW_HOME",
        tmp_path / "missing-openclaw",
    )
    monkeypatch.setattr(
        "agent_doctor.adapters.hermes.HERMES_HOME",
        tmp_path / "missing-hermes",
    )

    hosts = detect_hosts(use_cache=False)

    host_names = [h.capabilities().host_name for h in hosts]
    assert "generic" in host_names
    assert "openclaw" not in host_names
    assert "hermes" not in host_names


def test_detect_hosts_finds_openclaw_when_home_exists(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", tmp_path / "missing-hermes")

    hosts = detect_hosts(use_cache=False)
    host_names = [h.capabilities().host_name for h in hosts]
    assert "openclaw" in host_names


def test_detect_hosts_finds_hermes_when_home_exists(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", tmp_path / "missing-openclaw")
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", home)

    hosts = detect_hosts(use_cache=False)
    host_names = [h.capabilities().host_name for h in hosts]
    assert "hermes" in host_names
    assert "generic" in host_names  # always present


def test_detect_hosts_orders_real_hosts_before_generic(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.hermes.HERMES_HOME", tmp_path / "missing-hermes")

    hosts = detect_hosts(use_cache=False)
    host_names = [h.capabilities().host_name for h in hosts]
    assert host_names.index("openclaw") < host_names.index("generic")
