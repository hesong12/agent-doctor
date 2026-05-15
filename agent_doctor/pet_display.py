"""Desktop display for Agent Doctor.

This module is intentionally optional UI glue. It lazy-imports ``tkinter`` so
the scan/apply/autopilot production path remains dependency-free and headless
safe. The display reads ``pet-status.json`` and renders a small always-on-top
Agent Doctor window that refreshes as autopilot updates the file.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile, gettempdir
from typing import Any

_WINDOW_WIDTH = 260
_WINDOW_HEIGHT = 310
_ASSET_NAME = "doctor_pet.png"


@dataclass(frozen=True)
class DisplayOption:
    id: str
    label: str
    description: str
    command: str = ""


@dataclass(frozen=True)
class DisplayAction:
    id: str
    label: str
    command: str = ""


@dataclass(frozen=True)
class DisplayEvidence:
    file: str
    line: int
    role: str
    quote: str


@dataclass(frozen=True)
class DisplaySnapshot:
    platform: str
    phase: str
    state: str
    action: str
    severity: str
    headline: str
    message: str
    emotion_message: str
    diagnosis: str
    recommendation: str
    recovery_prompt: str
    expires_after_seconds: int
    session_id: str
    card_path: str
    primary_label: str
    primary_command: str
    latest_event_id: str
    latest_trigger: str
    dismiss_state_path: str
    evidence: tuple[DisplayEvidence, ...]
    options: tuple[DisplayOption, ...]
    fill: str
    accent: str


def default_status_file() -> Path:
    return Path("~/.agent-doctor/pet/pet-status.json").expanduser()


def user_sprite_path() -> Path:
    """Where ``pet-set-sprite`` writes the user's custom sprite."""

    return Path("~/.agent-doctor/pet/sprite.png").expanduser()


def packaged_sprite_path() -> Path | None:
    """Path to the packaged default doctor sprite, or ``None`` if missing."""

    path = Path(__file__).with_name("assets") / _ASSET_NAME
    if path.exists():
        return path
    return None


def pet_asset_path() -> Path | None:
    """Resolve the sprite to display, preferring the user's custom override."""

    custom = user_sprite_path()
    if custom.exists():
        return custom
    return packaged_sprite_path()


def read_status_payload(status_file: Path) -> dict[str, Any]:
    path = status_file.expanduser()
    if not path.exists():
        return {
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "headline": "Agent Doctor is waiting for status.",
            "message": f"Status file not found yet: {path}",
            "session_id": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "phase": "healthy",
            "headline": "Agent Doctor is waiting for a valid status.",
            "message": f"Status file is temporarily unreadable: {exc}",
            "diagnosis": "Agent Doctor could not read the latest status update.",
            "recommendation": "Keep Agent Doctor running. The next valid status write will refresh this panel.",
            "session_id": "",
        }
    if not isinstance(data, dict):
        return {
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "phase": "healthy",
            "headline": "Agent Doctor is waiting for a valid status.",
            "message": "Expected a JSON object.",
            "diagnosis": "Agent Doctor could not use the latest status update.",
            "recommendation": "Keep Agent Doctor running. The next valid status write will refresh this panel.",
            "session_id": "",
        }
    return data


def snapshot_from_payload(payload: dict[str, Any]) -> DisplaySnapshot:
    state = str(payload.get("state") or "idle")
    action = str(payload.get("action") or "silent")
    severity = str(payload.get("severity") or "low")
    evidence = _display_evidence(payload)
    options = _display_options(payload)
    primary_label, primary_command = _primary_option(options)
    if state == "intervening":
        fill = "#f8d3d0"
        accent = "#b42318"
    elif state == "concerned":
        fill = "#fdecc8"
        accent = "#b54708"
    elif state == "watching":
        fill = "#d8e8ff"
        accent = "#175cd3"
    else:
        fill = "#e7f0ff"
        accent = "#3556c7"
    return DisplaySnapshot(
        platform=str(payload.get("platform") or "generic"),
        phase=str(payload.get("phase") or "healthy"),
        state=state,
        action=action,
        severity=severity,
        headline=str(payload.get("headline") or "Agent Doctor is idle."),
        message=str(payload.get("message") or ""),
        emotion_message=str(payload.get("emotion_message") or ""),
        diagnosis=str(payload.get("diagnosis") or ""),
        recommendation=str(payload.get("recommendation") or ""),
        recovery_prompt=str(payload.get("recovery_prompt") or ""),
        expires_after_seconds=_int_payload(payload, "expires_after_seconds", 120),
        session_id=str(payload.get("session_id") or ""),
        card_path=str(payload.get("card_path") or ""),
        primary_label=primary_label,
        primary_command=primary_command,
        latest_event_id=str(payload.get("latest_event_id") or ""),
        latest_trigger=str(payload.get("latest_trigger") or ""),
        dismiss_state_path=str(payload.get("dismiss_state_path") or ""),
        evidence=evidence,
        options=options,
        fill=fill,
        accent=accent,
    )


def apply_transient_overlay(snapshot: DisplaySnapshot) -> DisplaySnapshot:
    """Return a snapshot whose ``state`` is overlaid by the transient file if any.

    All other fields are preserved. Animation accent/fill colours are picked
    from a per-state table when the transient overlays a state.
    """

    from . import pet_transient as _pt

    payload = _pt.read_transient()
    if not payload:
        return snapshot
    state = payload.get("state")
    accent, fill = _transient_visuals(state)
    return replace(
        snapshot,
        state=state,
        accent=accent or snapshot.accent,
        fill=fill or snapshot.fill,
    )


def _transient_visuals(state: str) -> tuple[str | None, str | None]:
    if state == "listening":
        return "#33b3a8", "#e0fbf8"
    if state == "thinking":
        return "#e0a040", "#fff5d6"
    return None, None


def _int_payload(payload: dict[str, Any], key: str, fallback: int) -> int:
    try:
        return int(payload.get(key) or fallback)
    except (TypeError, ValueError):
        return fallback


def _display_evidence(payload: dict[str, Any]) -> tuple[DisplayEvidence, ...]:
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        return ()
    parsed: list[DisplayEvidence] = []
    for item in evidence[:3]:
        if not isinstance(item, dict):
            continue
        line_value = item.get("line")
        try:
            line = int(line_value)
        except (TypeError, ValueError):
            line = 0
        parsed.append(
            DisplayEvidence(
                file=str(item.get("file") or ""),
                line=line,
                role=str(item.get("role") or ""),
                quote=str(item.get("quote") or ""),
            )
        )
    return tuple(parsed)


def _display_options(payload: dict[str, Any]) -> tuple[DisplayOption, ...]:
    options = payload.get("options")
    if not isinstance(options, list):
        return ()
    parsed: list[DisplayOption] = []
    for item in options:
        if not isinstance(item, dict):
            continue
        option_id = str(item.get("id") or "").strip()
        if not option_id:
            continue
        label = str(item.get("label") or option_id.replace("_", " ").title()).strip()
        parsed.append(
            DisplayOption(
                id=option_id,
                label=label or option_id,
                description=str(item.get("description") or ""),
                command=str(item.get("command") or ""),
            )
        )
    return tuple(parsed)


def _primary_option(options: tuple[DisplayOption, ...]) -> tuple[str, str]:
    selected = next(
        (item for item in options if item.id == "stage_fix"),
        options[0] if options else None,
    )
    if selected is None:
        return ("Stage repair", "")
    return (selected.label, selected.command)


def _option_by_id(snapshot: DisplaySnapshot, option_id: str) -> DisplayOption | None:
    for option in snapshot.options:
        if option.id == option_id:
            return option
    return None


def _command_is_runnable(command: str) -> bool:
    stripped = command.strip()
    return bool(stripped) and "<" not in stripped and ">" not in stripped


def _snapshot_uses_chinese(snapshot: DisplaySnapshot) -> bool:
    text = "\n".join(
        (
            snapshot.headline,
            snapshot.message,
            snapshot.emotion_message,
            snapshot.diagnosis,
            snapshot.recommendation,
            " ".join(item.quote for item in snapshot.evidence),
        )
    )
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _display_actions(snapshot: DisplaySnapshot) -> tuple[DisplayAction, ...]:
    actions: list[DisplayAction] = []
    seen: set[str] = set()
    chinese = _snapshot_uses_chinese(snapshot)
    if snapshot.state in ("concerned", "intervening"):
        actions.append(DisplayAction(id="dismiss_for_now", label="知道了" if chinese else "Got it"))
        return tuple(actions)
    for option in snapshot.options:
        if option.id == "start_autopilot":
            continue
        if option.id in seen or not _command_is_runnable(option.command):
            continue
        actions.append(DisplayAction(id=option.id, label=option.label, command=option.command))
        seen.add(option.id)
    close_label = "关闭" if chinese else "Close"
    actions.append(DisplayAction(id="dismiss_for_now", label=close_label))
    actions.append(DisplayAction(id="quit_pet", label="退出" if chinese else "Quit"))
    if snapshot.card_path:
        actions.append(DisplayAction(id="open_card", label="Open status card"))
    return tuple(actions)


def _can_send_recovery(snapshot: DisplaySnapshot) -> bool:
    if snapshot.platform != "openclaw":
        return False
    if snapshot.state not in {"concerned", "intervening"}:
        return False
    if not snapshot.evidence:
        return False
    source = snapshot.evidence[0].file
    return bool(source and source != "<manual>")


def _issue_title(snapshot: DisplaySnapshot) -> str:
    chinese = _snapshot_uses_chinese(snapshot)
    if snapshot.headline:
        return snapshot.headline
    if snapshot.latest_trigger == "user_frustration_signal":
        return "检测到用户不满" if chinese else "User frustration detected"
    if snapshot.latest_trigger == "completion_claim_without_nearby_verification":
        return "完成声明需要验证" if chinese else "Completion claim needs verification"
    if snapshot.latest_trigger == "tool_failure_or_hidden_error":
        return "工具失败需要处理" if chinese else "Tool failure needs acknowledgement"
    return _state_label(snapshot)


def _evidence_text(snapshot: DisplaySnapshot) -> str:
    chinese = _snapshot_uses_chinese(snapshot)
    if not snapshot.evidence:
        return "当前状态没有包含 transcript 证据。" if chinese else "No transcript evidence was included in this status."
    if snapshot.latest_trigger == "tool_failure_or_hidden_error":
        return "工具输出里出现了失败或错误信号。" if chinese else "Tool output contains failure or error language."
    item = snapshot.evidence[0]
    source = _evidence_source_label(item)
    if item.line and item.file and item.file != "<manual>":
        source = f"{source}:{item.line}" if source else f"line {item.line}"
    role = item.role.title() if item.role else "Evidence"
    quote = _shorten(item.quote, 180)
    return f'{role} quote: "{quote}"\nSource: {source}'


def _evidence_source_label(item: DisplayEvidence) -> str:
    if not item.file or item.file == "<manual>":
        return "Manual report"
    return item.file


def _expectation_text(snapshot: DisplaySnapshot) -> str:
    if snapshot.recommendation:
        return snapshot.recommendation
    if snapshot.latest_trigger == "user_frustration_signal":
        return (
            "The active agent should stop the normal success path, acknowledge the concrete "
            "failure, and give one evidence-backed recovery step."
        )
    if snapshot.latest_trigger == "completion_claim_without_nearby_verification":
        return (
            "The active agent should verify the claim before repeating success or saying the "
            "work is done."
        )
    if snapshot.latest_trigger == "tool_failure_or_hidden_error":
        return (
            "The active agent should surface the tool failure and adjust the plan before "
            "claiming progress."
        )
    if snapshot.message:
        return snapshot.message
    return "Review the concrete evidence before changing the current response."


def _user_action_text(snapshot: DisplaySnapshot) -> str:
    chinese = _snapshot_uses_chinese(snapshot)
    if snapshot.state in ("concerned", "intervening"):
        if chinese:
            return f"不用操作。点“知道了”会收起这次安慰；如果你不点，它会在 {snapshot.expires_after_seconds} 秒后自己安静退下。"
        return (
            f"No action is needed. Got it hides this comfort moment; otherwise it fades after {snapshot.expires_after_seconds} seconds."
        )
    has_runnable_action = any(
        action.command for action in _display_actions(snapshot) if action.id != "dismiss_for_now"
    )
    if has_runnable_action:
        return "Use a repair/open action if you want Agent Doctor to stage reviewable follow-up work."
    if snapshot.card_path:
        return "Open the status card for details, or hide this alert after you have seen it."
    return (
        "No extra input is needed in this panel. Use the issue and evidence above to correct "
        "the active agent response, then hide this alert."
    )


def _recovery_prompt(snapshot: DisplaySnapshot) -> str:
    if snapshot.recovery_prompt:
        return snapshot.recovery_prompt
    if snapshot.state not in ("concerned", "intervening"):
        return ""
    return "\n".join(
        [
            "Agent Doctor detected a live quality issue.",
            "",
            "Concrete evidence:",
            _evidence_text(snapshot),
            "",
            "Do this now:",
            _expectation_text(snapshot),
            "",
            "Do not continue the normal success path until the failure is acknowledged and the next corrective step is clear.",
        ]
    )


def _detail_sections(snapshot: DisplaySnapshot) -> tuple[tuple[str, str], ...]:
    sections: list[tuple[str, str]] = []
    if snapshot.emotion_message:
        sections.append(("安慰" if _snapshot_uses_chinese(snapshot) else "Comfort", snapshot.emotion_message))
    sections.append(("场景" if _snapshot_uses_chinese(snapshot) else "Scene", snapshot.diagnosis or _issue_title(snapshot)))
    sections.append(("现场一句" if _snapshot_uses_chinese(snapshot) else "What it saw", _evidence_text(snapshot)))
    sections.append(("Your choices", _user_action_text(snapshot)))
    return tuple(sections)


def _dialog_detail_text(snapshot: DisplaySnapshot) -> str:
    lines: list[str] = []
    if snapshot.session_id:
        lines.append(f"Session: {snapshot.session_id}")
    for title, body in _detail_sections(snapshot):
        if lines:
            lines.append("")
        lines.append(f"{title}:")
        lines.append(body)
    return "\n".join(lines)


def _snapshot_event_key(snapshot: DisplaySnapshot) -> str:
    if snapshot.latest_event_id:
        return snapshot.latest_event_id
    return "|".join((snapshot.state, snapshot.session_id, snapshot.headline))


def snapshot_to_dict(snapshot: DisplaySnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    data["actions"] = [asdict(action) for action in _display_actions(snapshot)]
    data["recovery_prompt"] = snapshot.recovery_prompt
    return data


def _write_snapshot_status_file(snapshot: DisplaySnapshot) -> Path:
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="agent-doctor-send-",
        suffix=".json",
        delete=False,
    ) as handle:
        json.dump(snapshot_to_dict(snapshot), handle, ensure_ascii=False)
        return Path(handle.name)


def _state_label(snapshot: DisplaySnapshot) -> str:
    if snapshot.phase == "comforting":
        return "小医生陪着" if _snapshot_uses_chinese(snapshot) else "Comforting"
    if snapshot.phase == "advice_ready":
        return "Suggestion ready"
    if snapshot.phase == "diagnosing":
        return "Diagnosing"
    if snapshot.state == "intervening":
        return "Intervention needed"
    if snapshot.state == "concerned":
        return "Needs review"
    if snapshot.state == "watching":
        return "Watching"
    if snapshot.state == "listening":
        return "Listening…"
    if snapshot.state == "thinking":
        return "Optimizing prompt…"
    return "Idle"


def _visible_snapshot(
    snapshot: DisplaySnapshot,
    interaction: dict[str, Any],
    now: float,
) -> DisplaySnapshot:
    event_key = _snapshot_event_key(snapshot)
    if interaction.get("seen_event") != event_key:
        interaction["seen_event"] = event_key
        interaction["seen_at"] = now
        if interaction.get("dismissed_event") != event_key:
            interaction["bubble"] = False
    if not _snapshot_expired(snapshot, float(interaction.get("seen_at") or now), now):
        return snapshot
    return replace(
        snapshot,
        state="idle",
        action="silent",
        severity="low",
        phase="healthy",
        headline="Agent Doctor is watching.",
        message="The previous alert quieted after inactivity.",
        emotion_message="",
        diagnosis="No active visible incident. Agent Doctor will wake again when it sees a new frustration signal.",
        recommendation="Keep working normally.",
        recovery_prompt="",
        fill="#e7f0ff",
        accent="#3556c7",
    )


def _snapshot_expired(snapshot: DisplaySnapshot, first_seen: float, now: float) -> bool:
    if snapshot.state not in {"concerned", "intervening"}:
        return False
    if snapshot.expires_after_seconds <= 0:
        return False
    return now - first_seen >= snapshot.expires_after_seconds


def _pet_action_detail(stdout: str, stderr: str) -> str:
    text = (stdout or stderr or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _shorten(text.replace("\n", " "), 320)
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("mode") or "").strip()
    return _shorten(text.replace("\n", " "), 320)


def display_pet(
    status_file: Path | None = None,
    *,
    poll_seconds: float = 1.0,
    topmost: bool = True,
) -> None:
    """Open an always-on-top Agent Doctor window and refresh from status JSON."""

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except ImportError as exc:  # pragma: no cover - environment-specific
        if platform.system() == "Darwin" and shutil.which("swift"):
            _display_pet_appkit(
                status_file or default_status_file(),
                poll_seconds=poll_seconds,
                topmost=topmost,
                asset_path=pet_asset_path(),
            )
            return
        raise RuntimeError(
            "Agent Doctor desktop display requires tkinter, or Swift/AppKit on macOS. "
            "Use `agent-doctor pet --out <dir>` to render files without a desktop window."
        ) from exc

    status_path = (status_file or default_status_file()).expanduser()
    poll_interval = max(0.2, poll_seconds)

    root = tk.Tk()
    root.title("Agent Doctor")
    root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}+120+120")
    root.resizable(False, False)
    root.overrideredirect(True)
    root.configure(bg="#ff00ff")
    if topmost:
        root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", "#ff00ff")
    except Exception:
        pass

    canvas = tk.Canvas(
        root,
        width=_WINDOW_WIDTH,
        height=_WINDOW_HEIGHT,
        bg="#ff00ff",
        highlightthickness=0,
    )
    canvas.pack(fill="both", expand=True)

    # Hot-reload state: re-resolve the sprite path on every tick so a fresh
    # `pet-set-sprite` is reflected without restarting the window.
    sprite_state: dict[str, Any] = {
        "path": None,
        "mtime": 0.0,
        "image": None,
    }

    def _reload_pet_image_if_changed() -> Any:
        path = pet_asset_path()
        if path is None:
            sprite_state["path"] = None
            sprite_state["mtime"] = 0.0
            sprite_state["image"] = None
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return sprite_state["image"]
        if (
            sprite_state["image"] is not None
            and sprite_state["path"] == path
            and sprite_state["mtime"] == mtime
        ):
            return sprite_state["image"]
        try:
            raw_image = tk.PhotoImage(file=str(path))
            scaled = raw_image.subsample(3, 3)
        except Exception:
            sprite_state["path"] = path
            sprite_state["mtime"] = mtime
            sprite_state["image"] = None
            return None
        sprite_state["path"] = path
        sprite_state["mtime"] = mtime
        sprite_state["image"] = scaled
        return scaled

    _reload_pet_image_if_changed()
    drag = {"x": 0, "y": 0}
    interaction = {
        "moved": False,
        "bubble": False,
        "dismissed_event": "",
        "seen_event": "",
        "seen_at": time.monotonic(),
    }
    dialog: dict[str, Any] = {"window": None}

    def start_drag(event: Any) -> None:
        drag["x"] = event.x
        drag["y"] = event.y
        interaction["moved"] = False

    def move_drag(event: Any) -> None:
        interaction["moved"] = True
        root.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

    def finish_click(event: Any) -> None:
        if not interaction["moved"]:
            interaction["bubble"] = not interaction["bubble"]

    def open_status_card(snapshot: DisplaySnapshot) -> None:
        if not snapshot.card_path:
            return
        if platform.system() == "Darwin":
            subprocess.run(["open", snapshot.card_path], check=False)
        elif os.name == "nt":
            os.startfile(snapshot.card_path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", snapshot.card_path], check=False)

    def show_message(title: str, body: str) -> None:
        messagebox.showinfo(title, body, parent=root)

    def dismiss_snapshot(snapshot: DisplaySnapshot) -> None:
        command = [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet-action",
            "dismiss",
            "--status-file",
            str(status_path),
        ]
        interaction["bubble"] = False
        interaction["dismissed_event"] = _snapshot_event_key(snapshot)

        def worker() -> None:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            payload = read_status_payload(status_path) if result.returncode == 0 else None
            detail = _pet_action_detail(result.stdout, result.stderr)

            def finish() -> None:
                if result.returncode != 0:
                    interaction["bubble"] = True
                    interaction["dismissed_event"] = ""
                    show_message("Dismiss not saved", detail or "Agent Doctor could not persist this dismissal.")
                    return
                if payload is not None:
                    status_cache["snapshot"] = snapshot_from_payload(payload)
                    status_cache["read_at"] = time.monotonic()

            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def send_recovery_to_agent(snapshot: DisplaySnapshot, popup: Any | None = None) -> None:
        snapshot_status_path = _write_snapshot_status_file(snapshot)
        command = [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet-action",
            "send-recovery",
            "--status-file",
            str(snapshot_status_path),
        ]

        def worker() -> None:
            try:
                result = subprocess.run(command, text=True, capture_output=True, check=False)
            finally:
                snapshot_status_path.unlink(missing_ok=True)
            detail = _pet_action_detail(result.stdout, result.stderr)

            def finish() -> None:
                if result.returncode == 0:
                    dismiss_snapshot(snapshot)
                    if popup is not None:
                        popup.destroy()
                    show_message("Suggestion sent", detail or "The active agent received the recovery suggestion.")
                    return
                show_message("Suggestion not sent", detail or "Agent Doctor could not route this incident.")

            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def run_command_action(action: DisplayAction) -> None:
        if action.command:
            subprocess.Popen(
                ["/bin/sh", "-lc", action.command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            show_message(f"{action.label} started", action.command)

    def perform_dialog_action(action: DisplayAction, snapshot: DisplaySnapshot, popup: Any) -> None:
        if action.id == "tell_current_agent":
            send_recovery_to_agent(snapshot, popup)
            return
        if action.id == "open_card":
            open_status_card(snapshot)
            return
        if action.id == "dismiss_for_now":
            dismiss_snapshot(snapshot)
            popup.destroy()
            return
        if action.id == "quit_pet":
            root.destroy()
            return
        run_command_action(action)

    def open_status_dialog(snapshot: DisplaySnapshot) -> None:
        existing = dialog.get("window")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                dialog["window"] = None

        popup = tk.Toplevel(root)
        dialog["window"] = popup
        popup.title("Agent Doctor")
        popup.resizable(False, False)
        if topmost:
            popup.attributes("-topmost", True)
        try:
            popup.transient(root)
        except Exception:
            pass
        popup.configure(bg="#ffffff")
        x = root.winfo_x() + 18
        y = root.winfo_y() + _WINDOW_HEIGHT + 8
        popup.geometry(f"420x360+{x}+{y}")
        popup.protocol("WM_DELETE_WINDOW", popup.destroy)

        frame = tk.Frame(popup, bg="#ffffff", padx=14, pady=12)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text=_state_label(snapshot),
            fg=snapshot.accent,
            bg="#ffffff",
            font=("Helvetica", 11, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            frame,
            text=_issue_title(snapshot),
            fg="#111827",
            bg="#ffffff",
            font=("Helvetica", 14, "bold"),
            anchor="w",
            justify="left",
            wraplength=390,
        ).pack(fill="x", pady=(6, 0))
        if snapshot.session_id:
            tk.Label(
                frame,
                text=f"Session: {snapshot.session_id}",
                fg="#6b7280",
                bg="#ffffff",
                font=("Helvetica", 9),
                anchor="w",
            ).pack(fill="x", pady=(6, 0))
        for title, body in _detail_sections(snapshot):
            tk.Label(
                frame,
                text=title,
                fg="#111827",
                bg="#ffffff",
                font=("Helvetica", 10, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(10, 0))
            tk.Label(
                frame,
                text=body,
                fg="#374151",
                bg="#ffffff",
                font=("Helvetica", 10),
                anchor="w",
                justify="left",
                wraplength=390,
            ).pack(fill="x", pady=(2, 0))

        buttons = tk.Frame(frame, bg="#ffffff")
        buttons.pack(fill="x", pady=(12, 0))
        for index, action in enumerate(_display_actions(snapshot)):
            side = "right" if action.id == "dismiss_for_now" else "left"
            padx = (8, 0) if index else (0, 0)
            tk.Button(
                buttons,
                text=action.label,
                command=lambda item=action: perform_dialog_action(item, snapshot, popup),
            ).pack(side=side, padx=padx)

    def change_sprite() -> None:
        path = filedialog.askopenfilename(
            parent=root,
            title="Choose a sprite for Agent Doctor",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        command = [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet-set-sprite",
            path,
        ]

        def worker() -> None:
            result = subprocess.run(command, text=True, capture_output=True, check=False)

            def finish() -> None:
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    show_message(
                        "Could not set sprite",
                        detail or "agent-doctor pet-set-sprite failed.",
                    )

            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    context_menu = tk.Menu(root, tearoff=0)
    context_menu.add_command(label="Change sprite…", command=change_sprite)

    def show_context_menu(event: Any) -> None:
        try:
            context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            context_menu.grab_release()

    canvas.bind("<ButtonPress-1>", start_drag)
    canvas.bind("<B1-Motion>", move_drag)
    canvas.bind("<ButtonRelease-1>", finish_click)
    # Right-click: macOS sends Button-2, Linux/Windows send Button-3.
    canvas.bind("<Button-2>", show_context_menu)
    canvas.bind("<Button-3>", show_context_menu)
    canvas.bind("<Control-Button-1>", show_context_menu)

    status_cache: dict[str, Any] = {
        "read_at": 0.0,
        "snapshot": snapshot_from_payload(read_status_payload(status_path)),
    }

    def active_snapshot() -> DisplaySnapshot:
        return _visible_snapshot(status_cache["snapshot"], interaction, time.monotonic())

    def draw() -> None:
        now = time.monotonic()
        if now - float(status_cache["read_at"]) >= poll_interval:
            status_cache["snapshot"] = snapshot_from_payload(read_status_payload(status_path))
            status_cache["read_at"] = now
            # Sprite changes are user-driven and rare; only stat() at the
            # status-poll cadence (default 1 s) instead of every animation tick.
            _reload_pet_image_if_changed()
        raw_snapshot = status_cache["snapshot"]
        snapshot = _visible_snapshot(raw_snapshot, interaction, now)
        snapshot = apply_transient_overlay(snapshot)
        canvas.delete("all")
        _draw_pet(canvas, snapshot, phase=now, pet_image=sprite_state["image"])
        _draw_tk_state_chip(canvas, snapshot)
        auto_show = (
            snapshot.state in ("concerned", "intervening")
            and _snapshot_event_key(snapshot) != interaction["dismissed_event"]
        )
        if interaction["bubble"] or auto_show:
            _draw_tk_bubble(canvas, snapshot)
        canvas.after(66, draw)

    draw()
    root.mainloop()


def _draw_pet(
    canvas: Any,
    snapshot: DisplaySnapshot,
    *,
    phase: float = 0.0,
    pet_image: Any | None = None,
) -> None:
    canvas.create_rectangle(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT, fill="#ff00ff", outline="")
    if pet_image is not None:
        _draw_sprite_pet(canvas, snapshot, phase=phase, pet_image=pet_image)
        return
    _draw_vector_pet(canvas, snapshot, phase=phase)


def _draw_tk_bubble(canvas: Any, snapshot: DisplaySnapshot) -> None:
    headline = _shorten(_issue_title(snapshot), 54)
    message = _shorten(snapshot.emotion_message or snapshot.message or _evidence_text(snapshot), 126)
    canvas.create_oval(14, 8, 246, 142, fill="#fff7ed", outline="#111827", width=2)
    canvas.create_polygon(108, 138, 130, 160, 152, 138, fill="#fff7ed", outline="#111827")
    canvas.create_oval(210, 20, 224, 34, fill="#fde68a", outline="#111827", width=1)
    canvas.create_oval(226, 38, 236, 48, fill="#14b8a6", outline="#111827", width=1)
    canvas.create_text(
        26,
        25,
        text=_state_label(snapshot),
        fill="#f97316",
        font=("Helvetica", 12, "bold"),
        anchor="w",
        width=208,
    )
    canvas.create_text(
        26,
        50,
        text=headline,
        fill="#111827",
        font=("Helvetica", 12, "bold"),
        anchor="w",
        width=208,
    )
    canvas.create_text(
        26,
        82,
        text=message,
        fill="#374151",
        font=("Helvetica", 11),
        anchor="w",
        width=208,
    )
    canvas.create_text(
        26,
        122,
        text="Click for a tiny hug" if not _snapshot_uses_chinese(snapshot) else "点一下，收个小抱抱",
        fill="#f97316",
        font=("Helvetica", 11, "bold"),
        anchor="w",
        width=208,
    )


def _draw_tk_state_chip(canvas: Any, snapshot: DisplaySnapshot) -> None:
    if snapshot.state == "idle":
        return
    label = _state_label(snapshot)
    canvas.create_rectangle(28, 264, 232, 296, fill="#ffffff", outline=snapshot.accent, width=2)
    canvas.create_oval(43, 276, 53, 286, fill=snapshot.accent, outline="")
    canvas.create_text(
        64,
        281,
        text=label,
        fill="#111827",
        font=("Helvetica", 11, "bold"),
        anchor="w",
        width=150,
    )


def _draw_sprite_pet(
    canvas: Any,
    snapshot: DisplaySnapshot,
    *,
    phase: float,
    pet_image: Any,
) -> None:
    bob = _bob_for_state(snapshot.state, phase)
    center_x = _WINDOW_WIDTH / 2
    center_y = 194 - bob

    _draw_tk_effects(canvas, snapshot, phase)
    shadow_width = 66 + (5 * math.sin(phase * 2.0))
    canvas.create_oval(
        center_x - shadow_width / 2,
        288,
        center_x + shadow_width / 2,
        304,
        fill="#111827",
        outline="",
        stipple="gray50",
    )
    canvas.create_image(center_x, center_y, image=pet_image)
    if snapshot.state == "watching":
        scan_y = 172 + (18 * ((math.sin(phase * 3.2) + 1) / 2))
        canvas.create_line(56, scan_y, 134, scan_y, fill="#9ff6ff", width=3)
    elif snapshot.state == "concerned":
        ring = 20 + (10 * ((math.sin(phase * 4.0) + 1) / 2))
        canvas.create_oval(
            center_x - ring,
            222 - ring,
            center_x + ring,
            222 + ring,
            outline=snapshot.accent,
            width=2,
        )
    elif snapshot.state == "intervening":
        canvas.create_oval(178, 152, 207, 181, fill=snapshot.accent, outline="#ffffff", width=2)
        canvas.create_text(192, 165, text="!", fill="#ffffff", font=("Helvetica", 17, "bold"))
        pulse = 3 + (8 * ((math.sin(phase * 5.5) + 1) / 2))
        canvas.create_oval(
            178 - pulse,
            152 - pulse,
            207 + pulse,
            181 + pulse,
            outline=snapshot.accent,
            width=2,
        )


def _draw_tk_effects(canvas: Any, snapshot: DisplaySnapshot, phase: float) -> None:
    from . import pet_animations as _pa

    canvas.delete(_pa.ANIMATION_TAG)

    if snapshot.state == "listening":
        _pa.draw_listening(
            canvas,
            t=phase,
            cx=_WINDOW_WIDTH / 2,
            cy=194,
        )
        return
    if snapshot.state == "thinking":
        _pa.draw_thinking(
            canvas,
            t=phase,
            cx=_WINDOW_WIDTH / 2,
            cy=194,
        )
        return

    pulse = (math.sin(phase * 2.0) + 1) / 2
    x_offset = 35
    y_offset = 92
    if snapshot.state == "idle":
        canvas.create_oval(
            48 + x_offset,
            36 + y_offset,
            142 + x_offset,
            134 + y_offset,
            fill="#dbeafe",
            outline="",
        )
    elif snapshot.state == "watching":
        canvas.create_oval(
            35 + x_offset,
            28 + y_offset,
            155 + x_offset,
            148 + y_offset,
            outline="#60a5fa",
            width=2,
        )
        x = 48 + x_offset + (94 * pulse)
        canvas.create_oval(x - 4, 27 + y_offset, x + 4, 35 + y_offset, fill="#9ff6ff", outline="")
    elif snapshot.state == "concerned":
        size = 104 + (18 * pulse)
        canvas.create_oval(
            95 + x_offset - size / 2,
            88 + y_offset - size / 2,
            95 + x_offset + size / 2,
            88 + y_offset + size / 2,
            outline="#f59e0b",
            width=3,
        )
    elif snapshot.state == "intervening":
        size = 108 + (16 * pulse)
        canvas.create_oval(
            95 + x_offset - size / 2,
            86 + y_offset - size / 2,
            95 + x_offset + size / 2,
            86 + y_offset + size / 2,
            outline="#f97316",
            width=2,
        )
        for index, (dx, dy, color) in enumerate(((30, 10, "#fde68a"), (130, 30, "#14b8a6"), (40, 132, "#f97316"))):
            bob = 8 * ((math.sin(phase * (2.4 + index)) + 1) / 2)
            canvas.create_oval(
                x_offset + dx,
                y_offset + dy - bob,
                x_offset + dx + 12,
                y_offset + dy + 12 - bob,
                fill=color,
                outline="#111827",
                width=1,
            )


def _bob_for_state(state: str, phase: float) -> float:
    if state == "intervening":
        return 4.0 + (2.0 * math.sin(phase * 8.0))
    if state == "concerned":
        return 2.5 + (1.5 * math.sin(phase * 4.0))
    if state == "watching":
        return 3.0 + (2.0 * math.sin(phase * 2.8))
    return 2.0 + (1.2 * math.sin(phase * 1.8))


def _draw_vector_pet(canvas: Any, snapshot: DisplaySnapshot, *, phase: float) -> None:
    bob = _bob_for_state(snapshot.state, phase)

    # Soft shadow.
    canvas.create_oval(54, 178, 128, 196, fill="#1f2937", outline="", stipple="gray50")

    # Cloud hair / doctor cap silhouette.
    canvas.create_oval(36, 28 - bob, 90, 88 - bob, fill="#88aaff", outline="#111827", width=2)
    canvas.create_oval(74, 22 - bob, 136, 88 - bob, fill="#9bb8ff", outline="#111827", width=2)
    canvas.create_oval(24, 55 - bob, 74, 110 - bob, fill="#6f90ed", outline="#111827", width=2)
    canvas.create_oval(120, 58 - bob, 162, 108 - bob, fill="#7698f2", outline="#111827", width=2)
    canvas.create_rectangle(68, 40 - bob, 114, 58 - bob, fill="#ffffff", outline="#111827", width=2)
    canvas.create_text(91, 49 - bob, text="+", fill=snapshot.accent, font=("Helvetica", 14, "bold"))

    # Face screen.
    canvas.create_rectangle(50, 72 - bob, 132, 119 - bob, fill="#f4fff7", outline="#375b71", width=3)
    eye_color = "#348b88" if snapshot.state != "intervening" else "#ffffff"
    canvas.create_arc(70, 90 - bob, 86, 105 - bob, start=200, extent=140, style="arc", outline=eye_color, width=3)
    canvas.create_arc(100, 90 - bob, 116, 105 - bob, start=200, extent=140, style="arc", outline=eye_color, width=3)

    # Body / coat.
    canvas.create_rectangle(64, 128 - bob, 118, 178 - bob, fill=snapshot.fill, outline="#111827", width=3)
    canvas.create_line(91, 130 - bob, 91, 177 - bob, fill="#cbd5e1", width=2)
    canvas.create_line(76, 140 - bob, 89, 153 - bob, fill="#cbd5e1", width=2)
    canvas.create_line(106, 140 - bob, 93, 153 - bob, fill="#cbd5e1", width=2)
    canvas.create_oval(79, 145 - bob, 103, 169 - bob, outline=snapshot.accent, width=3)
    canvas.create_line(91, 169 - bob, 91, 177 - bob, fill=snapshot.accent, width=3)

    # Arms / legs.
    canvas.create_line(63, 140 - bob, 42, 166 - bob, fill="#5b7ee5", width=11)
    canvas.create_line(119, 140 - bob, 140, 166 - bob, fill="#5b7ee5", width=11)
    canvas.create_line(78, 178 - bob, 74, 199 - bob, fill="#5b7ee5", width=12)
    canvas.create_line(104, 178 - bob, 108, 199 - bob, fill="#5b7ee5", width=12)

    if snapshot.state == "intervening":
        canvas.create_oval(136, 26, 160, 50, fill=snapshot.accent, outline="#ffffff", width=2)
        canvas.create_text(148, 38, text="!", fill="#ffffff", font=("Helvetica", 15, "bold"))


def _shorten(value: str, limit: int = 24) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def _display_pet_appkit(
    status_file: Path,
    *,
    poll_seconds: float,
    topmost: bool,
    asset_path: Path | None = None,
) -> None:
    source_path = Path(gettempdir()) / "agent_doctor_pet_display.swift"
    source_path.write_text(_appkit_source(), encoding="utf-8")

    user_sprite = user_sprite_path()
    packaged_sprite = packaged_sprite_path()

    command = [
        "swift",
        str(source_path),
        str(status_file.expanduser()),
        str(max(0.2, poll_seconds)),
        "1" if topmost else "0",
        str(asset_path.expanduser()) if asset_path is not None else "",
        sys.executable,
        # Pass both candidate sprite paths so the AppKit hot-reload loop can
        # re-resolve "user override exists?" each tick and switch from the
        # packaged sprite to a freshly-installed user sprite without restart.
        str(user_sprite),
        str(packaged_sprite) if packaged_sprite is not None else "",
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Swift/AppKit Agent Doctor exited with rc={completed.returncode}")


def _appkit_source() -> str:
    """Load the AppKit desktop pet implementation from the packaged Swift asset."""

    source_path = Path(__file__).with_name("assets") / "pet_display.swift"
    return source_path.read_text(encoding="utf-8")
