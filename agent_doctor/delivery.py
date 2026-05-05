"""Host-native delivery adapters for Agent Doctor autopilot events."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class OpenClawSystemEventResult:
    delivered: bool
    skipped: bool
    command: list[str]
    stdout: str = ""
    stderr: str = ""


def default_openclaw_notify_command() -> str:
    """Command suitable for autopilot's --notify-command hook."""

    return f"{shlex.quote(sys.executable)} -m agent_doctor.cli notify openclaw-system-event"


def notify_openclaw_system_event(
    *,
    env: Mapping[str, str] | None = None,
    openclaw_bin: str = "openclaw",
    mode: str = "now",
    timeout_ms: int = 30000,
    include_card_chars: int = 6000,
    all_actions: bool = False,
    dry_run: bool = False,
) -> OpenClawSystemEventResult:
    values = dict(env or os.environ)
    action = values.get("AGENT_DOCTOR_ACTION", "")
    if action != "intervene" and not all_actions:
        return OpenClawSystemEventResult(delivered=False, skipped=True, command=[])

    text = render_openclaw_system_event_text(
        values,
        include_card_chars=include_card_chars,
    )
    command = [
        openclaw_bin,
        "system",
        "event",
        "--mode",
        mode,
        "--timeout",
        str(timeout_ms),
        "--text",
        text,
    ]
    run_env = _openclaw_subprocess_env(values)
    if dry_run:
        return OpenClawSystemEventResult(delivered=False, skipped=False, command=command)

    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=max(5, timeout_ms / 1000 + 5),
            env=run_env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"openclaw binary not found: {openclaw_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"openclaw system event timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"openclaw system event could not start: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "openclaw system event failed").strip())
    return OpenClawSystemEventResult(
        delivered=True,
        skipped=False,
        command=command,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def render_openclaw_system_event_text(
    env: Mapping[str, str],
    *,
    include_card_chars: int = 6000,
) -> str:
    card_path = env.get("AGENT_DOCTOR_CARD", "")
    card_text = _read_card(card_path, include_card_chars=include_card_chars)
    payload = {
        "event_id": env.get("AGENT_DOCTOR_EVENT_ID", ""),
        "trigger": env.get("AGENT_DOCTOR_TRIGGER", ""),
        "severity": env.get("AGENT_DOCTOR_SEVERITY", ""),
        "action": env.get("AGENT_DOCTOR_ACTION", ""),
        "session_id": env.get("AGENT_DOCTOR_SESSION_ID", ""),
        "card": card_path,
        "summary": env.get("AGENT_DOCTOR_SUMMARY", ""),
    }
    return "\n".join(
        [
            "AGENT DOCTOR INTERVENTION",
            "",
            "The local Agent Doctor sidecar emitted a high-severity intervention.",
            "Treat this as live recovery input for the current session.",
            "",
            "Event metadata:",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "",
            "Required response behavior:",
            "- Pause the normal success path.",
            "- Name the concrete failure using the card evidence.",
            "- State the corrective action briefly.",
            "- Do not defend the prior response or write a long apology.",
            "",
            "Diagnosis card:",
            card_text or "(card unavailable; use the event metadata above)",
        ]
    )


def _read_card(path: str, *, include_card_chars: int) -> str:
    if not path:
        return ""
    if include_card_chars <= 0:
        return ""
    card_path = Path(path).expanduser()
    try:
        with card_path.open("r", encoding="utf-8") as handle:
            text = handle.read(include_card_chars + 1)
    except (OSError, ValueError):
        return ""
    if len(text) <= include_card_chars:
        return text
    return text[:include_card_chars] + "\n... [truncated]"


def _openclaw_subprocess_env(values: Mapping[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(values)
    host_home = values.get("AGENT_DOCTOR_HOST_HOME")
    if host_home:
        env["HOME"] = host_home
        env["AGENT_DOCTOR_HOST_HOME"] = host_home
    return env
