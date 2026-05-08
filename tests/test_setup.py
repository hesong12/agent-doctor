import plistlib
import subprocess
import sys
from pathlib import Path

from agent_doctor.setup import render_autopilot_setup_result, setup_autopilot


def test_setup_autopilot_detects_hosts_bootstraps_and_installs_services(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("agent_doctor.setup.host_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service.service_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service._service_kind", lambda: "launchd")
    calls: list[list[str]] = []

    def fake_run(command, text, capture_output):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_doctor.service.subprocess.run", fake_run)
    (tmp_path / ".openclaw" / "agents" / "main" / "sessions").mkdir(parents=True)
    (tmp_path / ".hermes" / "sessions").mkdir(parents=True)

    result = setup_autopilot(interval=2, cooldown_seconds=5)

    assert result.home == tmp_path
    assert result.bootstrap is not None
    assert {host.target for host in result.bootstrap.installed()} >= {"openclaw", "hermes"}
    assert {target.platform for target in result.installed()} == {"openclaw", "hermes"}
    assert len(calls) == 9  # bootout/bootstrap/kickstart for each sidecar plus desktop pet

    for target in result.installed():
        assert target.service is not None
        payload = plistlib.loads(target.service.service_file.read_bytes())
        args = payload["ProgramArguments"]
        assert args[:4] == [sys.executable, "-m", "agent_doctor.cli", "autopilot"]
        assert "--watch" in args
        assert "--changed-only" in args
        assert "--pet-out" in args
        assert args[args.index("--pet-out") + 1] == str(tmp_path / ".agent-doctor" / "pet")
        assert "--inbox-dir" not in args
        assert "--notify-command" not in args
        assert target.service.started is True

    assert result.pet_service is not None
    pet_payload = plistlib.loads(result.pet_service.service_file.read_bytes())
    assert pet_payload["KeepAlive"] is False
    assert pet_payload["ProgramArguments"][:4] == [
        sys.executable,
        "-m",
        "agent_doctor.cli",
        "pet-display",
    ]


def test_setup_autopilot_skips_missing_hosts_without_force(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.setup.host_home", lambda: tmp_path)

    result = setup_autopilot(bootstrap_hosts=False, start=False)

    assert result.installed() == []
    assert {target.platform for target in result.targets} == {"openclaw", "hermes"}
    assert all(target.skipped_reason for target in result.targets)


def test_setup_autopilot_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.setup.host_home", lambda: tmp_path)
    (tmp_path / ".openclaw" / "agents" / "main" / "sessions").mkdir(parents=True)

    result = setup_autopilot(platforms=["openclaw"], dry_run=True)
    text = render_autopilot_setup_result(result)

    assert "[dry-run] openclaw" in text
    assert "would install service" in text
    assert "pet      would install desktop Agent Doctor service" in text
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_setup_autopilot_preserves_explicit_notify_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.setup.host_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service.service_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service._service_kind", lambda: "launchd")
    monkeypatch.setattr(
        "agent_doctor.service.subprocess.run",
        lambda command, text, capture_output: subprocess.CompletedProcess(command, 0, "", ""),
    )
    (tmp_path / ".openclaw" / "agents" / "main" / "sessions").mkdir(parents=True)

    result = setup_autopilot(
        platforms=["openclaw"],
        notify_command="/tmp/custom-notify",
    )

    target = result.installed()[0]
    assert target.service is not None
    args = plistlib.loads(target.service.service_file.read_bytes())["ProgramArguments"]
    assert args[args.index("--notify-command") + 1] == "/tmp/custom-notify"


def test_cli_setup_autopilot_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_DOCTOR_HOST_HOME", str(tmp_path))
    (tmp_path / ".openclaw" / "agents" / "main" / "sessions").mkdir(parents=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "setup",
            "autopilot",
            "--platform",
            "openclaw",
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Agent Doctor — autopilot setup" in result.stdout
    assert "[dry-run] openclaw" in result.stdout
    assert "does not edit OpenClaw/Hermes runtime configuration" in result.stdout
