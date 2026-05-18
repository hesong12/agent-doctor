"""Widget composition for the redesigned Hotkey tab (layout B).

Pure tk/ttk layout. State + apply lives in ``hotkey_tab``. Capture overlay
lives in ``hotkey_capture``. Permission detection in ``permission_probe``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_doctor import hotkey_install as hi
from agent_doctor import hotkey_parse as hp
from agent_doctor.ui.preferences import hotkey_capture as hc
from agent_doctor.ui.preferences import hotkey_tab as ht
from agent_doctor.ui.preferences import permission_probe as pp

_PILL_TEXT = {
    "listening": ("Listening", "#2a8a4a", "#e5f7eb"),
    "permission_needed": ("Permission needed", "#a45c10", "#fef0e0"),
    "paused": ("Paused", "#6a6a6a", "#ececec"),
    "daemon_stopped": ("Daemon stopped", "#6a6a6a", "#ececec"),
}

_LOG_PATH = Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()


def build(notebook: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(notebook, padding=14)
    notebook.add(frame, text="Hotkey")

    # --- header row -------------------------------------------------
    header = ttk.Frame(frame)
    header.pack(fill="x", pady=(0, 8))
    ttk.Label(header, text="Global hotkey", font=("Helvetica", 14, "bold")).pack(side="left")
    pill_var = tk.StringVar(value="…")
    pill_label = tk.Label(header, textvariable=pill_var, padx=8, pady=2)
    pill_label.pack(side="right")

    ttk.Label(frame, text="Trigger dictation from anywhere on the system.", foreground="#777").pack(
        anchor="w", pady=(0, 8)
    )

    # --- permission banner -----------------------------------------
    # Start hidden — _refresh() packs/forgets based on whether a permission
    # is actually missing. ttk.Frame doesn't expose -background so we can't
    # camouflage it; hiding the whole frame is cleaner.
    banner_frame = ttk.Frame(frame)
    banner_text = tk.StringVar(value="")
    banner_label = tk.Label(banner_frame, textvariable=banner_text, anchor="w", padx=8, pady=6)
    banner_label.pack(side="left", fill="x", expand=True)
    banner_action = ttk.Button(banner_frame, text="Open settings…")
    banner_action.pack(side="right", padx=6)

    # --- shortcut tile ---------------------------------------------
    tile = ttk.LabelFrame(frame, text="Shortcut")
    tile.pack(fill="x", pady=(0, 12))
    binding_var = tk.StringVar(value="")
    binding_label = tk.Label(tile, textvariable=binding_var, font=("Helvetica", 16))
    binding_label.pack(side="left", padx=12, pady=10)
    hint_label = tk.Label(tile, text="Hold to record · click Record to change", foreground="#777")
    hint_label.pack(side="left", padx=4, pady=10)
    record_btn = ttk.Button(tile, text="Record…")
    record_btn.pack(side="right", padx=6, pady=10)
    test_btn = ttk.Button(tile, text="Test")
    test_btn.pack(side="right", padx=2, pady=10)

    # --- mode segmented --------------------------------------------
    mode_frame = ttk.Frame(frame)
    mode_frame.pack(fill="x", pady=(0, 10))
    ttk.Label(mode_frame, text="Mode").pack(side="left")
    ptt_var = tk.BooleanVar(value=True)
    ptt_radio = ttk.Radiobutton(mode_frame, text="Push-to-talk", variable=ptt_var, value=True)
    toggle_radio = ttk.Radiobutton(mode_frame, text="Toggle", variable=ptt_var, value=False)
    ptt_radio.pack(side="left", padx=8)
    toggle_radio.pack(side="left", padx=8)

    # --- daemon toggle ---------------------------------------------
    daemon_frame = ttk.Frame(frame)
    daemon_frame.pack(fill="x", pady=(0, 10))
    ttk.Label(daemon_frame, text="Background daemon").pack(side="left")
    daemon_var = tk.BooleanVar(value=False)
    daemon_chk = ttk.Checkbutton(daemon_frame, variable=daemon_var)
    daemon_chk.pack(side="right")

    # --- footer ----------------------------------------------------
    footer = ttk.Frame(frame)
    footer.pack(fill="x", pady=(10, 0))
    show_logs_btn = ttk.Button(footer, text="Show daemon logs")
    show_logs_btn.pack(side="left")
    uninstall_btn = ttk.Button(footer, text="Uninstall")
    uninstall_btn.pack(side="right")

    # ---------------- behaviour wiring -----------------------------

    def _refresh() -> None:
        snap = ht.daemon_status_snapshot()
        pill_key = str(snap["pill"])
        text, fg, bg = _PILL_TEXT[pill_key]
        pill_var.set(text)
        pill_label.configure(foreground=fg, background=bg)
        perms = snap["perms"]  # type: ignore[index]
        if getattr(perms, "first_missing", None):
            target = perms.first_missing
            label = {
                "accessibility": "⚠ Accessibility permission required",
                "input_monitoring": "⚠ Input Monitoring permission required",
            }[target]
            banner_text.set(label)
            banner_label.configure(background="#fff7e8", foreground="#7a5b14")
            banner_action.configure(command=lambda t=target: subprocess.run(["open", pp.settings_url(t)]))
            # Show the banner only when there's something to surface. We
            # repack right before the shortcut tile so the visual layout
            # (header → banner → tile → mode → daemon → footer) is stable.
            if not banner_frame.winfo_ismapped():
                banner_frame.pack(fill="x", pady=(0, 10), before=tile)
        else:
            banner_text.set("")
            banner_action.configure(command=lambda: None)
            if banner_frame.winfo_ismapped():
                banner_frame.pack_forget()
        s = snap["settings"]  # type: ignore[index]
        binding_var.set(_render_binding(str(s.binding)))
        ptt_var.set(bool(s.push_to_talk))
        chord = hp.parse(str(s.binding))
        if hp.is_modifier_only(chord):
            toggle_radio.state(["disabled"])
            ptt_var.set(True)
        else:
            toggle_radio.state(["!disabled"])
        daemon_var.set(bool(s.daemon_enabled and bool(snap["daemon"]["running"])))  # type: ignore[index]

    def _on_record() -> None:
        new_binding = _open_capture_overlay(frame)
        if new_binding is None:
            return
        ht.HotkeyState(binding=new_binding, push_to_talk=ptt_var.get()).apply()
        _refresh()

    def _on_test() -> None:
        snap = ht.daemon_status_snapshot()
        pill = snap["pill"]
        if pill == "listening":
            messagebox.showinfo(
                "Hotkey",
                "Daemon is running. Press the hotkey to confirm it triggers "
                "dictation — this button can't observe the global event stream "
                "directly.",
            )
        elif pill == "permission_needed":
            messagebox.showwarning(
                "Hotkey",
                "Daemon is running but a macOS permission is missing. Grant "
                "Accessibility / Input Monitoring, then try the hotkey.",
            )
        elif pill == "paused":
            messagebox.showwarning("Hotkey", "Daemon is paused — toggle Background daemon on.")
        else:
            messagebox.showwarning("Hotkey", "Daemon is not installed — toggle Background daemon on.")

    def _on_mode_change() -> None:
        current = ht.HotkeyState.from_settings().binding
        ht.HotkeyState(binding=current, push_to_talk=ptt_var.get()).apply()
        _refresh()

    def _on_daemon_toggle() -> None:
        from agent_doctor import dictate_settings as ds
        if daemon_var.get():
            # Turning ON: bootstrap if plist exists, else fresh install.
            try:
                if hi.DEFAULT_PLIST_PATH.exists():
                    hi.resume()
                else:
                    hi.install()
            except hi.HotkeyInstallError as exc:
                messagebox.showerror("Hotkey", str(exc))
                daemon_var.set(False)
                return
            s = ds.load()
            ds.save(ds.replace_section(
                s, hotkey=ds.HotkeySettings(
                    binding=s.hotkey.binding,
                    push_to_talk=s.hotkey.push_to_talk,
                    daemon_enabled=True,
                )
            ))
        else:
            # Turning OFF: bootout the agent but keep the plist (so we can
            # resume without a rebuild). Uninstall (separate button) is the
            # only path that removes the plist.
            hi.pause()
            s = ds.load()
            ds.save(ds.replace_section(
                s, hotkey=ds.HotkeySettings(
                    binding=s.hotkey.binding,
                    push_to_talk=s.hotkey.push_to_talk,
                    daemon_enabled=False,
                )
            ))
        _refresh()

    def _on_show_logs() -> None:
        subprocess.run(["open", "-a", "Console", str(_LOG_PATH)])

    def _on_uninstall() -> None:
        from agent_doctor import dictate_settings as ds
        if not messagebox.askyesno(
            "Stop and remove the hotkey daemon?",
            "This stops the LaunchAgent and deletes the helper. You can re-enable it any time.",
            icon=messagebox.WARNING,
        ):
            return
        hi.uninstall()
        try:
            hi.DEFAULT_HELPER_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        s = ds.load()
        ds.save(ds.replace_section(
            s, hotkey=ds.HotkeySettings(
                binding=s.hotkey.binding,
                push_to_talk=s.hotkey.push_to_talk,
                daemon_enabled=False,
            )
        ))
        daemon_var.set(False)
        _refresh()

    record_btn.configure(command=_on_record)
    test_btn.configure(command=_on_test)
    ptt_radio.configure(command=_on_mode_change)
    toggle_radio.configure(command=_on_mode_change)
    daemon_chk.configure(command=_on_daemon_toggle)
    show_logs_btn.configure(command=_on_show_logs)
    uninstall_btn.configure(command=_on_uninstall)

    def _poll() -> None:
        # Poll covers freshness — no separate focus-in binding needed;
        # bind_all was firing on every Tk focus change across all preference tabs.
        try:
            _refresh()
        finally:
            frame.after(1000, _poll)

    _refresh()
    frame.after(1000, _poll)


def _render_binding(canonical: str) -> str:
    glyphs = {
        "cmd": "⌘", "ctrl": "⌃", "option": "⌥", "shift": "⇧", "fn": "🌐",
        "right_cmd": "⌘ Right", "left_cmd": "⌘ Left",
        "right_option": "⌥ Right", "left_option": "⌥ Left",
        "right_ctrl": "⌃ Right", "left_ctrl": "⌃ Left",
        "right_shift": "⇧ Right", "left_shift": "⇧ Left",
    }
    if "+" not in canonical:
        return glyphs.get(canonical, canonical)
    parts = canonical.split("+")
    return " ".join(glyphs.get(p, p.capitalize()) for p in parts)


def _open_capture_overlay(parent: Any) -> str | None:
    """Tk overlay wrapping ``CaptureController``. Returns the canonical
    binding string on success, ``None`` on cancel/conflict.
    """

    import tkinter as tk

    dlg = tk.Toplevel(parent)
    dlg.title("Record hotkey")
    dlg.geometry("420x260")
    dlg.transient(parent)
    dlg.grab_set()

    controller = hc.CaptureController()
    headline = tk.StringVar(value="Press the key you want to hold")
    sub = tk.StringVar(value="Hold ⌘ ⌥ ⌃ ⇧ Fn, or press a chord.")
    cap_var = tk.StringVar(value="…")

    tk.Label(dlg, textvariable=headline, font=("Helvetica", 14, "bold")).pack(pady=(20, 4))
    tk.Label(dlg, textvariable=sub, foreground="#666").pack()
    tk.Label(dlg, textvariable=cap_var, font=("Helvetica", 22)).pack(pady=20)

    result: dict[str, str | None] = {"binding": None}

    def _commit_and_close() -> None:
        # For modifier-only bindings the controller has already auto-committed
        # on release; for chords we need an explicit commit().
        if controller.state is not hc.State.COMMITTED:
            try:
                controller.commit()
            except hc.CaptureBlocked:
                return  # conflict — leave the dialog open
        result["binding"] = controller.commit_result
        dlg.destroy()

    def _cancel_and_close(_event: Any = None) -> None:
        controller.cancel()
        dlg.destroy()

    def _sync_use_btn() -> None:
        # Only CAPTURED_CHORD makes "Use this chord" a valid commit target —
        # IDLE/CAPTURED_MODIFIER/CONFLICT all raise from controller.commit().
        # Modifier-only bindings auto-commit on release in _on_release, so
        # the button is only relevant for chord bindings.
        if controller.state is hc.State.CAPTURED_CHORD:
            use_btn.configure(state="normal")
        else:
            use_btn.configure(state="disabled")

    def _on_key(event: Any) -> None:
        token = _keysym_to_token(event.keysym)
        if token is None:
            return
        import time
        t_ms = int(time.monotonic() * 1000)
        controller.on_key_event(hc.KeyEvent(kind="press", key=token, t_ms=t_ms))
        cap_var.set(controller.captured or "…")
        if controller.state is hc.State.CONFLICT:
            sub.set(controller.conflict_reason or "Conflicts with macOS.")
        _sync_use_btn()

    def _on_release(event: Any) -> None:
        token = _keysym_to_token(event.keysym)
        if token is None:
            return
        import time
        t_ms = int(time.monotonic() * 1000)
        controller.on_key_event(hc.KeyEvent(kind="release", key=token, t_ms=t_ms))
        if controller.state is hc.State.COMMITTED:
            _commit_and_close()
            return
        _sync_use_btn()

    def _on_focus_out(event: Any) -> None:
        # Spec §5.2: window losing focus = Cancel. Guard against spurious
        # FocusOut events: (a) when a child widget gains focus inside the
        # same Toplevel (event.widget is not dlg), and (b) when Tk on macOS
        # delivers a FocusOut to the Toplevel with detail=NotifyInferior /
        # NotifyAncestor because focus moved to a descendant. Without the
        # detail check, clicking "Use this chord" cancels the dialog before
        # the button's command runs.
        if event.widget is not dlg:
            return
        detail = getattr(event, "detail", "")
        if detail in ("NotifyInferior", "NotifyAncestor"):
            return
        _cancel_and_close(event)

    dlg.bind("<Key>", _on_key)
    dlg.bind("<KeyRelease>", _on_release)
    dlg.bind("<Escape>", _cancel_and_close)
    dlg.bind("<FocusOut>", _on_focus_out)

    use_btn = tk.Button(dlg, text="Use this chord", command=_commit_and_close)
    use_btn.pack(pady=(4, 10))
    use_btn.configure(state="disabled")  # enabled only when state is CAPTURED_CHORD
    tk.Button(dlg, text="Cancel", command=_cancel_and_close).pack()

    dlg.focus_set()
    dlg.wait_window()
    return result["binding"]


_KEYSYM_TO_TOKEN = {
    "Meta_L": "left_cmd", "Meta_R": "right_cmd",
    "Alt_L": "left_option", "Alt_R": "right_option",
    "Control_L": "left_ctrl", "Control_R": "right_ctrl",
    "Shift_L": "left_shift", "Shift_R": "right_shift",
    # Generic modifier (no L/R surfacing from Tk) maps to chord-style tokens.
    "Control": "ctrl", "Shift": "shift", "Alt": "option", "Meta": "cmd",
    "space": "space", "Return": "return", "Escape": "escape", "Tab": "tab",
    "BackSpace": "backspace", "Delete": "delete",
}


def _keysym_to_token(keysym: str) -> str | None:
    if keysym in _KEYSYM_TO_TOKEN:
        return _KEYSYM_TO_TOKEN[keysym]
    if len(keysym) == 1 and keysym.isalnum():
        return keysym.lower()
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym.lower()
    return None
