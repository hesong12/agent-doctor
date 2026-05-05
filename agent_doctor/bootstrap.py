"""Auto-detect host agent frameworks and install the right skill into each.

This is the entry point for the "install agent-doctor" experience. A user
running ``pip install git+https://…/agent-doctor.git`` followed by
``agent-doctor bootstrap`` should end up with skill files in every memoryful
agent framework they already have on the machine (Hermes, OpenClaw, Claude
Code), plus an MCP config snippet they can paste into any MCP-aware host
that supports stdio servers.

We never modify a host agent's runtime configuration directly; the skill
files are inert Markdown SOPs. The MCP snippet is printed for the user to
copy. The trust boundary is the same as the rest of Agent Doctor: nothing
gets activated automatically.

Detection rules — keep these conservative. False detections that write a
skill file are mostly harmless (idempotent overwrite of a Markdown file in a
known location), but failing to detect cleanly is worse than detecting
nothing because users will conclude bootstrap is broken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .install import install_skill


@dataclass(frozen=True)
class HostInstall:
    """One detected (or potentially detectable) host agent framework."""

    name: str
    detected: bool
    target: str
    skill_dir: Path
    written_path: Path | None = None
    skipped_reason: str = ""


@dataclass(frozen=True)
class BootstrapResult:
    hosts: list[HostInstall]
    mcp_snippet: str

    def installed(self) -> list[HostInstall]:
        return [host for host in self.hosts if host.written_path is not None]


def detect_hosts(home: Path | None = None) -> list[HostInstall]:
    """Return one ``HostInstall`` per known host, with ``detected`` set."""

    home = (home or Path.home()).expanduser()
    candidates: list[HostInstall] = []

    hermes_root = home / ".hermes"
    candidates.append(
        HostInstall(
            name="Hermes",
            detected=hermes_root.exists(),
            target="hermes",
            skill_dir=hermes_root / "skills",
        )
    )

    openclaw_root = home / ".openclaw"
    candidates.append(
        HostInstall(
            name="OpenClaw",
            detected=openclaw_root.exists(),
            target="openclaw",
            skill_dir=openclaw_root / "agents" / "main" / "skills",
        )
    )

    claude_root = home / ".claude"
    candidates.append(
        HostInstall(
            name="Claude Code",
            detected=(claude_root / "skills").exists() or claude_root.exists(),
            target="claude-code",
            skill_dir=claude_root / "skills",
        )
    )

    return candidates


def bootstrap(
    *,
    home: Path | None = None,
    extra_targets: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> BootstrapResult:
    """Install skills into every detected host.

    ``force=True`` writes skills even into hosts whose root directory does
    not exist yet (creates them). ``dry_run=True`` reports what *would* be
    written without touching the filesystem. ``extra_targets`` adds explicit
    targets from the CLI alongside the auto-detected set.
    """

    detected = detect_hosts(home=home)
    extra = _build_extra(extra_targets, home=home or Path.home())
    seen_targets: set[str] = set()
    hosts: list[HostInstall] = []
    for host in detected + extra:
        if host.target in seen_targets:
            continue
        seen_targets.add(host.target)

        if not host.detected and not force and host.target in {"hermes", "openclaw"}:
            hosts.append(
                HostInstall(
                    name=host.name,
                    detected=False,
                    target=host.target,
                    skill_dir=host.skill_dir,
                    skipped_reason=f"{host.skill_dir.parent} not found; pass --force to install anyway.",
                )
            )
            continue

        if dry_run:
            hosts.append(
                HostInstall(
                    name=host.name,
                    detected=host.detected,
                    target=host.target,
                    skill_dir=host.skill_dir,
                    written_path=host.skill_dir / _expected_filename(host.target),
                    skipped_reason="dry-run",
                )
            )
            continue

        try:
            written = install_skill(host.target, host.skill_dir)
        except Exception as exc:  # pragma: no cover — surfaces unusual filesystem errors
            hosts.append(
                HostInstall(
                    name=host.name,
                    detected=host.detected,
                    target=host.target,
                    skill_dir=host.skill_dir,
                    skipped_reason=f"install failed: {exc}",
                )
            )
            continue
        hosts.append(
            HostInstall(
                name=host.name,
                detected=host.detected,
                target=host.target,
                skill_dir=host.skill_dir,
                written_path=written,
            )
        )

    return BootstrapResult(hosts=hosts, mcp_snippet=mcp_config_snippet())


def _build_extra(extra_targets: list[str] | None, *, home: Path) -> list[HostInstall]:
    if not extra_targets:
        return []
    candidates: list[HostInstall] = []
    for target in extra_targets:
        normalized = target.strip().casefold().replace("_", "-")
        if normalized == "claude":
            normalized = "claude-code"
        if normalized == "claude-code":
            candidates.append(
                HostInstall(
                    name="Claude Code (forced)",
                    detected=True,
                    target="claude-code",
                    skill_dir=home / ".claude" / "skills",
                )
            )
        elif normalized == "hermes":
            candidates.append(
                HostInstall(
                    name="Hermes (forced)",
                    detected=True,
                    target="hermes",
                    skill_dir=home / ".hermes" / "skills",
                )
            )
        elif normalized == "openclaw":
            candidates.append(
                HostInstall(
                    name="OpenClaw (forced)",
                    detected=True,
                    target="openclaw",
                    skill_dir=home / ".openclaw" / "agents" / "main" / "skills",
                )
            )
        elif normalized == "generic":
            candidates.append(
                HostInstall(
                    name="generic",
                    detected=True,
                    target="generic",
                    skill_dir=home / ".agent-doctor",
                )
            )
        else:
            raise ValueError(
                f"Unsupported target {target!r}. "
                "Expected one of: hermes, openclaw, claude-code, generic."
            )
    return candidates


def _expected_filename(target: str) -> str:
    return {
        "hermes": "agent-doctor/SKILL.md",
        "openclaw": "agent-doctor/SKILL.md",
        "claude-code": "agent-doctor/SKILL.md",
        "generic": "agent-doctor-skill.md",
    }.get(target, "agent-doctor-skill.md")


def mcp_config_snippet() -> str:
    """Return a JSON snippet a user can paste into an MCP-aware client.

    Points at ``agent-doctor mcp serve``, the stdio server defined in
    :mod:`agent_doctor.mcp`. The server itself requires the ``mcp`` extra
    (``pip install 'agent-doctor[mcp]'``); this snippet is harmless to paste
    even before the extra is installed — the host will surface a clear error
    on first connect attempt.
    """

    payload = {
        "mcpServers": {
            "agent-doctor": {
                "command": "agent-doctor",
                "args": ["mcp", "serve"],
                "env": {},
            }
        }
    }
    return json.dumps(payload, indent=2)


def render_bootstrap_summary(result: BootstrapResult) -> str:
    lines = ["Agent Doctor — bootstrap", ""]
    if not result.hosts:
        lines.append("No host agent frameworks detected and no targets requested.")
    else:
        lines.append("Hosts:")
        for host in result.hosts:
            if host.written_path is not None and host.skipped_reason != "dry-run":
                lines.append(f"  [installed] {host.name:<14} → {host.written_path}")
            elif host.skipped_reason == "dry-run":
                lines.append(f"  [dry-run]   {host.name:<14} would write {host.written_path}")
            elif host.detected:
                lines.append(
                    f"  [skipped]   {host.name:<14} ({host.skipped_reason or 'unknown'})"
                )
            else:
                lines.append(f"  [missing]   {host.name:<14} ({host.skipped_reason})")

    lines.extend(
        [
            "",
            "MCP configuration (paste into your MCP-aware client; requires `pip install 'agent-doctor[mcp]'`):",
            result.mcp_snippet,
            "",
            "Next steps:",
            "  agent-doctor scan --hermes --format markdown --out ./postmortem",
            "  agent-doctor apply --findings ./postmortem --out ./staging",
        ]
    )
    return "\n".join(lines)
