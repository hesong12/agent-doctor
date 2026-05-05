"""User-service installation for the Agent Doctor autopilot sidecar."""

from __future__ import annotations

import os
import platform as platform_module
import plistlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .autopilot import Platform
from .ingest import host_home
from .schema import Severity


@dataclass(frozen=True)
class ServiceResult:
    platform: Platform
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
    interval: float = 15.0,
    cooldown_seconds: int = 3600,
    min_severity: Severity = "medium",
    notify_command: str | None = None,
    inbox_dir: Path | None = None,
    name: str | None = None,
    start: bool = False,
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
    if start:
        start_commands, warnings = _start_service(service_file, service_kind)
        started = not warnings
    return ServiceResult(
        platform=platform,
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
    return command


def _write_launchd_plist(path: Path, command: list[str]) -> None:
    label = path.stem
    payload = {
        "Label": label,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(service_home() / ".agent-doctor" / "logs" / f"{label}.out.log"),
        "StandardErrorPath": str(service_home() / ".agent-doctor" / "logs" / f"{label}.err.log"),
        "EnvironmentVariables": {
            "AGENT_DOCTOR_HOST_HOME": str(service_home()),
        },
    }
    Path(payload["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def _write_systemd_unit(path: Path, command: list[str]) -> None:
    out_log = service_home() / ".agent-doctor" / "logs" / f"{path.stem}.log"
    out_log.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            "[Unit]",
            "Description=Agent Doctor autopilot sidecar",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=AGENT_DOCTOR_HOST_HOME={service_home()}",
            f"ExecStart={_systemd_exec(command)}",
            "Restart=always",
            "RestartSec=5",
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
