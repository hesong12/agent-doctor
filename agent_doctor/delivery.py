"""Host-native delivery adapters for Agent Doctor autopilot events."""

from __future__ import annotations

import json
import os
import shlex
import shutil
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


HOST_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
OPENCLAW_PROVIDER_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "PERPLEXITY_API_KEY",
        "VOYAGE_API_KEY",
    }
)


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
    run_env = _openclaw_subprocess_env(values)
    resolved_openclaw = resolve_openclaw_binary(openclaw_bin, env=run_env)
    command = [
        resolved_openclaw,
        "system",
        "event",
        "--mode",
        mode,
        "--timeout",
        str(timeout_ms),
        "--text",
        text,
    ]
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
        raise RuntimeError(f"openclaw binary not found: {resolved_openclaw}") from exc
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
    env["PATH"] = _with_host_bin_path(env.get("PATH", ""))
    host_home = values.get("AGENT_DOCTOR_HOST_HOME") or _host_home_from_env(env)
    env["HOME"] = host_home
    env["AGENT_DOCTOR_HOST_HOME"] = host_home
    env.update(_load_openclaw_provider_env(Path(host_home), existing=env))
    return env


def _load_openclaw_provider_env(
    host_home: Path,
    *,
    existing: Mapping[str, str],
) -> dict[str, str]:
    dotenv = host_home / ".openclaw" / ".env"
    try:
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    loaded: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in OPENCLAW_PROVIDER_ENV_KEYS or existing.get(key):
            continue
        loaded[key] = _dotenv_value(value.strip())
    return loaded


def _dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _host_home_from_env(env: Mapping[str, str]) -> str:
    home = Path(str(env.get("HOME") or str(Path.home()))).expanduser()
    parts = home.parts
    for marker in (".openclaw", ".hermes"):
        if marker in parts:
            index = parts.index(marker)
            if index > 0:
                return str(Path(*parts[:index]))
    return str(home)


def resolve_openclaw_binary(openclaw_bin: str = "openclaw", *, env: Mapping[str, str] | None = None) -> str:
    """Resolve OpenClaw in host-like environments such as launchd.

    launchd services often start with ``PATH=/usr/bin:/bin:/usr/sbin:/sbin``,
    while Homebrew installs OpenClaw under ``/opt/homebrew/bin`` on Apple
    Silicon. The delivery adapter is responsible for being host-native and
    should not rely on a user's interactive shell profile.
    """

    if "/" in openclaw_bin:
        path = Path(openclaw_bin).expanduser()
        if path.exists():
            return str(path)
        raise RuntimeError(f"openclaw binary not found: {openclaw_bin}")
    path_env = (env or os.environ).get("PATH", "")
    found = shutil.which(openclaw_bin, path=path_env)
    if found:
        return found
    for directory in HOST_BIN_DIRS:
        candidate = Path(directory) / openclaw_bin
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"openclaw binary not found: {openclaw_bin}")


def _with_host_bin_path(path: str) -> str:
    parts = [part for part in path.split(os.pathsep) if part]
    for directory in reversed(HOST_BIN_DIRS):
        if directory not in parts:
            parts.insert(0, directory)
    return os.pathsep.join(parts)
