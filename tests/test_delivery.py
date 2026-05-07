import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from agent_doctor.delivery import (
    default_openclaw_notify_command,
    notify_openclaw_system_event,
    render_openclaw_system_event_text,
    resolve_openclaw_binary,
)


def _event_env(card: Path) -> dict[str, str]:
    return {
        "AGENT_DOCTOR_HOST_HOME": str(card.parent),
        "AGENT_DOCTOR_EVENT_ID": "evt-1",
        "AGENT_DOCTOR_TRIGGER": "user_frustration_signal",
        "AGENT_DOCTOR_SEVERITY": "high",
        "AGENT_DOCTOR_ACTION": "intervene",
        "AGENT_DOCTOR_SESSION_ID": "session-1",
        "AGENT_DOCTOR_CARD": str(card),
        "AGENT_DOCTOR_SUMMARY": "Strong user frustration detected.",
    }


def test_default_openclaw_notify_command_quotes_current_python(monkeypatch) -> None:
    python = "/tmp/Python With Spaces/bin/python"
    monkeypatch.setattr("agent_doctor.delivery.sys.executable", python)

    command = default_openclaw_notify_command()

    assert command.startswith(shlex.quote(python))
    assert "agent_doctor.cli notify openclaw-system-event" in command


def test_render_openclaw_system_event_text_includes_card_and_required_behavior(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_text("# Agent Doctor Autopilot\n\nImmediate Agent Instruction", encoding="utf-8")

    text = render_openclaw_system_event_text(_event_env(card))

    assert "AGENT DOCTOR INTERVENTION" in text
    assert "Required response behavior" in text
    assert "Immediate Agent Instruction" in text
    assert "evt-1" in text


def test_render_openclaw_system_event_text_truncates_card_prefix(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_text("abcdef", encoding="utf-8")

    text = render_openclaw_system_event_text(_event_env(card), include_card_chars=3)
    diagnosis_card = text.split("Diagnosis card:", maxsplit=1)[1]

    assert "abc\n... [truncated]" in diagnosis_card
    assert "def" not in diagnosis_card


def test_render_openclaw_system_event_text_omits_card_when_limit_is_zero(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_text("hidden card body", encoding="utf-8")

    text = render_openclaw_system_event_text(_event_env(card), include_card_chars=0)
    diagnosis_card = text.split("Diagnosis card:", maxsplit=1)[1]

    assert "hidden card body" not in diagnosis_card
    assert "(card unavailable; use the event metadata above)" in diagnosis_card


def test_render_openclaw_system_event_text_ignores_malformed_card_encoding(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_bytes(b"\xff\xfe\xfa")

    text = render_openclaw_system_event_text(_event_env(card))
    diagnosis_card = text.split("Diagnosis card:", maxsplit=1)[1]

    assert "(card unavailable; use the event metadata above)" in diagnosis_card


def test_notify_openclaw_system_event_skips_non_interventions(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    env = _event_env(card) | {"AGENT_DOCTOR_ACTION": "notify"}

    result = notify_openclaw_system_event(env=env, dry_run=True)

    assert result.skipped is True
    assert result.command == []


def test_notify_openclaw_system_event_dry_run_builds_system_event_command(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")

    result = notify_openclaw_system_event(
        env=_event_env(card),
        openclaw_bin=str(openclaw),
        dry_run=True,
    )

    assert result.skipped is False
    assert result.delivered is False
    assert result.command[:4] == [str(openclaw), "system", "event", "--mode"]
    assert "--text" in result.command
    assert "AGENT DOCTOR INTERVENTION" in result.command[-1]


def test_notify_openclaw_system_event_uses_host_home_for_openclaw_cli(
    tmp_path: Path, monkeypatch
) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command, text, capture_output, timeout, env):
        captured["command"] = command
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("agent_doctor.delivery.subprocess.run", fake_run)

    result = notify_openclaw_system_event(
        env=_event_env(card),
        openclaw_bin=str(openclaw),
    )

    assert result.delivered is True
    assert result.stdout == "ok"
    assert captured["env"]["HOME"] == str(tmp_path)  # type: ignore[index]
    assert captured["env"]["AGENT_DOCTOR_HOST_HOME"] == str(tmp_path)  # type: ignore[index]
    assert captured["env"]["AGENT_DOCTOR_EVENT_ID"] == "evt-1"  # type: ignore[index]
    assert "/opt/homebrew/bin" in captured["env"]["PATH"]  # type: ignore[index]


def test_notify_openclaw_system_event_derives_host_home_from_sandbox_home(
    tmp_path: Path, monkeypatch
) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    sandbox_home = tmp_path / ".openclaw" / "agents" / "main" / "agent" / "codex-home" / "home"
    sandbox_home.mkdir(parents=True)
    captured: dict[str, object] = {}

    def fake_run(command, text, capture_output, timeout, env):
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.delenv("AGENT_DOCTOR_HOST_HOME", raising=False)
    monkeypatch.setattr("agent_doctor.delivery.subprocess.run", fake_run)

    env = _event_env(card)
    env.pop("AGENT_DOCTOR_HOST_HOME")
    env["HOME"] = str(sandbox_home)
    notify_openclaw_system_event(
        env=env,
        openclaw_bin=str(openclaw),
    )

    expected_home = Path(*sandbox_home.parts[: sandbox_home.parts.index(".openclaw")])
    assert captured["env"]["HOME"] == str(expected_home)  # type: ignore[index]
    assert captured["env"]["AGENT_DOCTOR_HOST_HOME"] == str(expected_home)  # type: ignore[index]


def test_notify_openclaw_system_event_incorporates_custom_process_env(
    tmp_path: Path, monkeypatch
) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command, text, capture_output, timeout, env):
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("agent_doctor.delivery.subprocess.run", fake_run)

    notify_openclaw_system_event(
        env=_event_env(card) | {"PATH": "/custom/bin"},
        openclaw_bin=str(openclaw),
    )

    assert captured["env"]["PATH"].endswith("/custom/bin")  # type: ignore[index]
    assert "/opt/homebrew/bin" in captured["env"]["PATH"]  # type: ignore[index]


def test_notify_openclaw_system_event_reports_missing_openclaw_binary(
    tmp_path: Path, monkeypatch
) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")

    def fake_run(command, text, capture_output, timeout, env):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr("agent_doctor.delivery.subprocess.run", fake_run)

    try:
        notify_openclaw_system_event(env=_event_env(card), openclaw_bin="/missing/openclaw")
    except RuntimeError as exc:
        assert "openclaw binary not found" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_notify_openclaw_system_event_reports_timeout(tmp_path: Path, monkeypatch) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run(command, text, capture_output, timeout, env):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr("agent_doctor.delivery.subprocess.run", fake_run)

    try:
        notify_openclaw_system_event(env=_event_env(card), openclaw_bin=str(openclaw))
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_cli_notify_openclaw_system_event_dry_run(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    env = os.environ.copy() | _event_env(card)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "notify",
            "openclaw-system-event",
            "--openclaw-bin",
            str(openclaw),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload["skipped"] is False
    assert payload["delivered"] is False
    assert payload["command"][:3] == [str(openclaw), "system", "event"]


def test_cli_notify_openclaw_system_event_missing_binary_has_clean_error(
    tmp_path: Path,
) -> None:
    card = tmp_path / "card.md"
    card.write_text("card", encoding="utf-8")
    env = os.environ.copy() | _event_env(card)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "notify",
            "openclaw-system-event",
            "--openclaw-bin",
            str(tmp_path / "missing-openclaw"),
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "agent-doctor: error: openclaw binary not found" in result.stderr
    assert "Traceback" not in result.stderr


def test_resolve_openclaw_binary_uses_host_paths_when_launchd_path_is_minimal(
    tmp_path: Path, monkeypatch
) -> None:
    fake_openclaw = tmp_path / "openclaw"
    fake_openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("agent_doctor.delivery.HOST_BIN_DIRS", (str(tmp_path),))

    resolved = resolve_openclaw_binary(
        "openclaw",
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )

    assert resolved == str(fake_openclaw)


def test_notify_openclaw_system_event_works_under_launchd_minimal_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Integration test reproducing the original launchd failure mode.

    With launchd's default PATH (/usr/bin:/bin:/usr/sbin:/sbin), bare
    'openclaw' is not findable. Before resolve_openclaw_binary, this
    failed with FileNotFoundError -> RuntimeError -> exit 1. After the
    fix, HOST_BIN_DIRS resolves the binary even with a stripped PATH.
    """
    fake_openclaw = tmp_path / "openclaw"
    fake_openclaw.write_text(
        "#!/bin/sh\necho ok\nexit 0\n",
        encoding="utf-8",
    )
    fake_openclaw.chmod(0o755)
    monkeypatch.setattr(
        "agent_doctor.delivery.HOST_BIN_DIRS",
        (str(tmp_path),),
    )

    card = tmp_path / "card.md"
    card.write_text("card body", encoding="utf-8")

    env = _event_env(card) | {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }

    result = notify_openclaw_system_event(env=env)

    assert result.delivered is True, (
        f"expected delivery, got skipped={result.skipped} stderr={result.stderr!r}"
    )
    assert result.command[0] == str(fake_openclaw), (
        "openclaw should be resolved to absolute path even with minimal PATH"
    )
    assert "ok" in result.stdout
