"""Opinionated setup flows for agents installing Agent Doctor for users."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .autopilot import Platform
from .bootstrap import BootstrapResult, bootstrap, detect_hosts
from .ingest import host_home
from .schema import Severity
from .service import ServiceResult, install_desktop_pet_service, install_sidecar_service

SetupPlatform = Literal["openclaw", "hermes"]


@dataclass(frozen=True)
class AutopilotSetupTarget:
    platform: SetupPlatform
    detected: bool
    transcript_path: Path
    out_dir: Path
    inbox_dir: Path | None
    service: ServiceResult | None = None
    skipped_reason: str = ""


@dataclass(frozen=True)
class AutopilotSetupResult:
    home: Path
    bootstrap: BootstrapResult | None
    targets: list[AutopilotSetupTarget] = field(default_factory=list)
    pet_service: ServiceResult | None = None
    desktop_pet: bool = True
    dry_run: bool = False

    def installed(self) -> list[AutopilotSetupTarget]:
        return [target for target in self.targets if target.service is not None]


def setup_autopilot(
    *,
    home: Path | None = None,
    platforms: list[SetupPlatform] | None = None,
    out_root: Path | None = None,
    inbox_root: Path | None = None,
    start: bool = True,
    bootstrap_hosts: bool = True,
    invalidate_cache: bool = True,
    force: bool = False,
    dry_run: bool = False,
    interval: float = 2.0,
    cooldown_seconds: int = 3600,
    min_severity: Severity = "high",
    notify_command: str | None = None,
    baseline_existing: bool = True,
    desktop_pet: bool = True,
) -> AutopilotSetupResult:
    """Install skills and sidecar services for every detected local host.

    This is the product-facing "AI agent can do it for the user" path. It is
    intentionally higher-level than ``service install``: detect OpenClaw /
    Hermes, bootstrap their skills, baseline existing transcripts, and start
    the right user services with safe defaults. It still stays outside the
    host runtime and never edits OpenClaw/Hermes configuration.
    """

    real_home = (home or host_home()).expanduser()
    selected = set(platforms or ["openclaw", "hermes"])
    out_base = (out_root or real_home / ".agent-doctor").expanduser()
    inbox_base = inbox_root.expanduser() if inbox_root is not None else None
    pet_dir = out_base / "pet"

    boot: BootstrapResult | None = None
    if bootstrap_hosts:
        boot = bootstrap(
            home=real_home,
            dry_run=dry_run,
            force=force,
            invalidate_cache=invalidate_cache,
        )

    detected = {host.target: host for host in detect_hosts(home=real_home)}
    targets: list[AutopilotSetupTarget] = []
    for platform in ("openclaw", "hermes"):
        if platform not in selected:
            continue
        target = detected.get(platform)
        is_detected = bool(target and target.detected)
        transcript_path = _default_transcript_path(real_home, platform)
        out_dir = out_base / platform
        inbox_dir = inbox_base / platform if inbox_base is not None else None

        if not is_detected and not force:
            targets.append(
                AutopilotSetupTarget(
                    platform=platform,
                    detected=False,
                    transcript_path=transcript_path,
                    out_dir=out_dir,
                    inbox_dir=inbox_dir,
                    skipped_reason=f"{_host_root(real_home, platform)} not found; pass --force to install anyway.",
                )
            )
            continue

        if dry_run:
            targets.append(
                AutopilotSetupTarget(
                    platform=platform,
                    detected=is_detected,
                    transcript_path=transcript_path,
                    out_dir=out_dir,
                    inbox_dir=inbox_dir,
                    skipped_reason="dry-run",
                )
            )
            continue

        service = install_sidecar_service(
            platform=_service_platform(platform),
            out_dir=out_dir,
            transcript_path=None,
            interval=interval,
            cooldown_seconds=cooldown_seconds,
            min_severity=min_severity,
            notify_command=_notify_command_for_platform(platform, notify_command),
            inbox_dir=inbox_dir,
            pet_out_dir=pet_dir,
            start=start,
            baseline_existing=baseline_existing,
        )
        targets.append(
            AutopilotSetupTarget(
                platform=platform,
                detected=is_detected,
                transcript_path=transcript_path,
                out_dir=out_dir,
                inbox_dir=inbox_dir,
                service=service,
            )
        )

    pet_service: ServiceResult | None = None
    if desktop_pet and not dry_run and any(target.service is not None for target in targets):
        pet_service = install_desktop_pet_service(
            status_file=pet_dir / "pet-status.json",
            start=start,
        )

    return AutopilotSetupResult(
        home=real_home,
        bootstrap=boot,
        targets=targets,
        pet_service=pet_service,
        desktop_pet=desktop_pet,
        dry_run=dry_run,
    )


def render_autopilot_setup_result(result: AutopilotSetupResult) -> str:
    lines = ["Agent Doctor — autopilot setup", "", f"Home: {result.home}", ""]
    if result.bootstrap is not None:
        installed = result.bootstrap.installed()
        if installed:
            lines.append("Skills:")
            for host in installed:
                status = "would install" if result.dry_run else "installed"
                lines.append(f"  [{status}] {host.name:<14} -> {host.written_path}")
        else:
            lines.append("Skills: no detected host skills installed.")
        if result.bootstrap.invalidations:
            lines.append("Cache invalidation:")
            for inv in result.bootstrap.invalidations:
                tag = "ok" if inv.succeeded else "failed"
                lines.append(f"  [{tag}] {inv.host:<14} {inv.detail}")
        lines.append("")

    if not result.targets:
        lines.append("Services: no OpenClaw/Hermes targets selected.")
        return "\n".join(lines)

    lines.append("Services:")
    for target in result.targets:
        if target.service is not None:
            started = "started" if target.service.started else "written"
            lines.append(f"  [{started}] {target.platform:<8} {target.service.service_file}")
            lines.append("    command: " + " ".join(target.service.command))
            for warning in target.service.warnings:
                lines.append(f"    warning: {warning}")
        elif target.skipped_reason == "dry-run":
            lines.append(f"  [dry-run] {target.platform:<8} would install service")
            lines.append(f"    transcripts: {target.transcript_path}")
            lines.append(f"    out: {target.out_dir}")
            lines.append(f"    inbox: {target.inbox_dir or 'disabled'}")
        else:
            lines.append(f"  [skipped] {target.platform:<8} {target.skipped_reason}")
    if result.dry_run and result.desktop_pet:
        lines.append("  [dry-run] pet      would install desktop Agent Doctor service")
    elif result.pet_service is not None:
        started = "started" if result.pet_service.started else "written"
        lines.append(f"  [{started}] pet      {result.pet_service.service_file}")
        lines.append("    command: " + " ".join(result.pet_service.command))
        for warning in result.pet_service.warnings:
            lines.append(f"    warning: {warning}")
    lines.extend(
        [
            "",
            "Boundary: setup only writes Agent Doctor skills, Agent Doctor state, and user-level Agent Doctor services.",
            "It does not edit OpenClaw/Hermes runtime configuration.",
        ]
    )
    return "\n".join(lines)


def _default_transcript_path(home: Path, platform: SetupPlatform) -> Path:
    if platform == "openclaw":
        return home / ".openclaw" / "agents" / "main" / "sessions"
    return home / ".hermes" / "sessions"


def _host_root(home: Path, platform: SetupPlatform) -> Path:
    if platform == "openclaw":
        return home / ".openclaw"
    return home / ".hermes"


def _service_platform(platform: SetupPlatform) -> Platform:
    return platform


def _notify_command_for_platform(platform: SetupPlatform, explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    return None
