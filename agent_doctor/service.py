"""User-service installation for the Agent Doctor autopilot sidecar."""

from __future__ import annotations

import os
import platform as platform_module
import plistlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .autopilot import Platform, baseline_autopilot_state
from .ingest import host_home
from .schema import Severity

HOST_BIN_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class ServiceResult:
    platform: str
    service_kind: str
    service_file: Path
    command: list[str]
    started: bool = False
    start_commands: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def install_sidecar_service(
    *,
    platform: Platform,
    out_dir: Path,
    transcript_path: Path | None = None,
    interval: float = 2.0,
    cooldown_seconds: int = 3600,
    min_severity: Severity = "medium",
    notify_command: str | None = None,
    inbox_dir: Path | None = None,
    pet_out_dir: Path | None = None,
    name: str | None = None,
    start: bool = False,
    baseline_existing: bool = True,
) -> ServiceResult:
    command = _autopilot_command(
        platform=platform,
        out_dir=out_dir,
        transcript_path=transcript_path,
        interval=interval,
        cooldown_seconds=cooldown_seconds,
        min_severity=min_severity,
        notify_command=notify_command,
        inbox_dir=inbox_dir,
        pet_out_dir=pet_out_dir,
        changed_only=baseline_existing,
    )
    service_file = expected_service_path(platform=platform, name=name)
    service_kind = _service_kind()
    warnings: list[str] = []
    service_file.parent.mkdir(parents=True, exist_ok=True)
    if service_kind == "launchd":
        _write_launchd_plist(service_file, command)
    elif service_kind == "systemd-user":
        _write_systemd_unit(service_file, command)
    else:
        raise ValueError(f"Unsupported service platform: {platform_module.system()}")

    start_commands: list[list[str]] = []
    started = False
    if baseline_existing:
        try:
            baseline_autopilot_state(
                platform=platform,
                path=transcript_path,
                out_dir=out_dir,
            )
        except Exception as exc:
            warnings.append(f"could not baseline existing transcripts: {exc}")
    if start:
        start_commands, start_warnings = _start_service(service_file, service_kind)
        warnings.extend(start_warnings)
        started = not start_warnings
    return ServiceResult(
        platform=platform,
        service_kind=service_kind,
        service_file=service_file,
        command=command,
        started=started,
        start_commands=start_commands,
        warnings=warnings,
    )


def install_desktop_pet_service(
    *,
    status_file: Path | None = None,
    poll_seconds: float = 1.0,
    topmost: bool = True,
    name: str = "pet",
    start: bool = False,
) -> ServiceResult:
    """Install the desktop Doctor Pet as the default user-facing service."""

    from .pet_display import default_status_file

    service_kind = _service_kind()
    service_file = expected_pet_service_path(name=name)
    command = _pet_display_command(
        status_file=status_file or default_status_file(),
        poll_seconds=poll_seconds,
        topmost=topmost,
    )
    service_file.parent.mkdir(parents=True, exist_ok=True)
    if service_kind == "launchd":
        _write_launchd_plist(service_file, command, keep_alive=False)
    elif service_kind == "systemd-user":
        _write_systemd_unit(
            service_file,
            command,
            description="Agent Doctor desktop pet",
            restart=False,
        )
    else:
        raise ValueError(f"Unsupported service platform: {platform_module.system()}")

    start_commands: list[list[str]] = []
    warnings: list[str] = []
    started = False
    if start:
        start_commands, warnings = _start_service(service_file, service_kind)
        started = not warnings
    return ServiceResult(
        platform="pet",
        service_kind=service_kind,
        service_file=service_file,
        command=command,
        started=started,
        start_commands=start_commands,
        warnings=warnings,
    )


def expected_service_path(*, platform: Platform, name: str | None = None) -> Path:
    suffix = _safe_suffix(name or platform)
    if _service_kind() == "launchd":
        return service_home() / "Library" / "LaunchAgents" / f"com.agentdoctor.{suffix}.plist"
    return service_home() / ".config" / "systemd" / "user" / f"agent-doctor-{suffix}.service"


def expected_pet_service_path(*, name: str = "pet") -> Path:
    suffix = _safe_suffix(name)
    if _service_kind() == "launchd":
        return service_home() / "Library" / "LaunchAgents" / f"com.agentdoctor.{suffix}.plist"
    return service_home() / ".config" / "systemd" / "user" / f"agent-doctor-{suffix}.service"


def service_home() -> Path:
    return host_home()


def render_service_result(result: ServiceResult) -> str:
    lines = [
        f"Wrote {result.service_kind} service: {result.service_file}",
        "Command:",
        "  " + " ".join(_quote(part) for part in result.command),
    ]
    if result.start_commands:
        lines.append("Start commands:")
        for command in result.start_commands:
            lines.append("  " + " ".join(_quote(part) for part in command))
    if result.started:
        lines.append("Service started.")
    for warning in result.warnings:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def _autopilot_command(
    *,
    platform: Platform,
    out_dir: Path,
    transcript_path: Path | None,
    interval: float,
    cooldown_seconds: int,
    min_severity: Severity,
    notify_command: str | None,
    inbox_dir: Path | None,
    pet_out_dir: Path | None,
    changed_only: bool,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_doctor.cli",
        "autopilot",
        "--platform",
        platform,
        "--out",
        str(out_dir.expanduser()),
        "--watch",
        "--interval",
        str(interval),
        "--cooldown-seconds",
        str(cooldown_seconds),
        "--min-severity",
        min_severity,
    ]
    if transcript_path is not None:
        command.extend(["--path", str(transcript_path.expanduser())])
    if notify_command:
        command.extend(["--notify-command", notify_command])
    if inbox_dir is not None:
        command.extend(["--inbox-dir", str(inbox_dir.expanduser())])
    if pet_out_dir is not None:
        command.extend(["--pet-out", str(pet_out_dir.expanduser())])
    if changed_only:
        command.append("--changed-only")
    return command


def _pet_display_command(
    *,
    status_file: Path,
    poll_seconds: float,
    topmost: bool,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_doctor.cli",
        "pet-display",
        "--status-file",
        str(status_file.expanduser()),
        "--poll",
        str(poll_seconds),
    ]
    if not topmost:
        command.append("--not-topmost")
    return command


def _write_launchd_plist(path: Path, command: list[str], *, keep_alive: bool = True) -> None:
    label = path.stem
    payload = {
        "Label": label,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
        "StandardOutPath": str(service_home() / ".agent-doctor" / "logs" / f"{label}.out.log"),
        "StandardErrorPath": str(service_home() / ".agent-doctor" / "logs" / f"{label}.err.log"),
        "EnvironmentVariables": {
            "AGENT_DOCTOR_HOST_HOME": str(service_home()),
            "PATH": HOST_BIN_PATH,
        },
    }
    Path(payload["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def _write_systemd_unit(
    path: Path,
    command: list[str],
    *,
    description: str = "Agent Doctor autopilot sidecar",
    restart: bool = True,
) -> None:
    out_log = service_home() / ".agent-doctor" / "logs" / f"{path.stem}.log"
    out_log.parent.mkdir(parents=True, exist_ok=True)
    restart_lines = ["Restart=always", "RestartSec=5"] if restart else ["Restart=no"]
    text = "\n".join(
        [
            "[Unit]",
            f"Description={description}",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=AGENT_DOCTOR_HOST_HOME={service_home()}",
            f"Environment=PATH={HOST_BIN_PATH}",
            f"ExecStart={_systemd_exec(command)}",
            *restart_lines,
            f"StandardOutput=append:{out_log}",
            f"StandardError=append:{out_log}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")


def _start_service(path: Path, service_kind: str) -> tuple[list[list[str]], list[str]]:
    warnings: list[str] = []
    commands: list[list[str]] = []
    if service_kind == "launchd":
        uid = str(os.getuid())
        commands = [
            ["launchctl", "bootout", f"gui/{uid}", str(path)],
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{path.stem}"],
        ]
    else:
        commands = [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", path.name],
        ]
    for index, command in enumerate(commands):
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode != 0:
            # launchctl bootout returns non-zero when the service was not loaded
            # yet. That is harmless before bootstrap.
            if service_kind == "launchd" and index == 0:
                continue
            detail = (result.stderr or result.stdout or "").strip()
            warnings.append(f"{' '.join(command)} failed: {detail}")
            break
    return commands, warnings


def _service_kind() -> str:
    system = platform_module.system()
    if system == "Darwin":
        return "launchd"
    if system == "Linux":
        return "systemd-user"
    return "unsupported"


def _safe_suffix(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip().lower())
    return cleaned or "default"


def _quote(value: str) -> str:
    if not value or any(ch.isspace() for ch in value):
        return repr(value)
    return value


def _systemd_exec(command: list[str]) -> str:
    return " ".join(_quote(part) for part in command)
