import plistlib
import subprocess
import sys
from pathlib import Path

from agent_doctor.service import install_desktop_pet_service, install_sidecar_service


def test_service_install_writes_launchd_plist_without_starting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.service.service_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service._service_kind", lambda: "launchd")
    sessions = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "old.jsonl").write_text('{"role":"user","content":"not useful"}\n', encoding="utf-8")

    result = install_sidecar_service(
        platform="openclaw",
        out_dir=tmp_path / "doctor",
        transcript_path=sessions,
        interval=3,
        cooldown_seconds=7,
        inbox_dir=tmp_path / "inbox",
        start=False,
    )

    assert result.service_kind == "launchd"
    assert result.service_file.exists()
    payload = plistlib.loads(result.service_file.read_bytes())
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    args = payload["ProgramArguments"]
    assert args[:4] == [sys.executable, "-m", "agent_doctor.cli", "autopilot"]
    assert "--watch" in args
    assert "--inbox-dir" in args
    assert "--changed-only" in args
    assert payload["EnvironmentVariables"]["AGENT_DOCTOR_HOST_HOME"] == str(tmp_path)
    assert "/opt/homebrew/bin" in payload["EnvironmentVariables"]["PATH"]
    assert (tmp_path / "doctor" / "state.sqlite3").exists()


def test_service_install_writes_systemd_unit_without_starting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.service.service_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service._service_kind", lambda: "systemd-user")

    result = install_sidecar_service(
        platform="hermes",
        out_dir=tmp_path / "doctor",
        interval=5,
        notify_command="echo ok",
        start=False,
    )

    text = result.service_file.read_text(encoding="utf-8")
    assert "[Service]" in text
    assert "agent_doctor.cli autopilot" in text
    assert "--platform hermes" in text
    assert "--notify-command 'echo ok'" in text
    assert "Restart=always" in text


def test_service_install_start_runs_platform_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.service.service_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service._service_kind", lambda: "systemd-user")
    calls: list[list[str]] = []

    def fake_run(command, text, capture_output):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agent_doctor.service.subprocess.run", fake_run)

    result = install_sidecar_service(
        platform="generic",
        transcript_path=tmp_path / "sessions",
        out_dir=tmp_path / "doctor",
        start=True,
    )

    assert result.started is True
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", result.service_file.name],
    ]


def test_desktop_pet_service_is_launchd_run_at_load_without_keepalive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("agent_doctor.service.service_home", lambda: tmp_path)
    monkeypatch.setattr("agent_doctor.service._service_kind", lambda: "launchd")

    result = install_desktop_pet_service(
        status_file=tmp_path / ".agent-doctor" / "pet" / "pet-status.json",
        start=False,
    )

    payload = plistlib.loads(result.service_file.read_bytes())
    args = payload["ProgramArguments"]
    assert result.platform == "pet"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is False
    assert args[:4] == [sys.executable, "-m", "agent_doctor.cli", "pet-display"]
    assert "--status-file" in args
    assert str(tmp_path / ".agent-doctor" / "pet" / "pet-status.json") in args
