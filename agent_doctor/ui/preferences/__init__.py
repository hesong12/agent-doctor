"""Preferences window root.

Opens a singleton tkinter Toplevel with five tabs. The widget code lives here
because it's small and shared across tabs; per-tab *logic* (validation,
settings I/O) lives in the sibling tab modules so it can be unit-tested headless.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import TabController  # re-export

__all__ = ["TabController", "open_window"]

_window: Optional[Any] = None


def open_window() -> None:
    """Open the Preferences window. Singleton — second invocation raises focus."""

    import tkinter as tk
    from tkinter import ttk

    from . import dictation_tab as dt
    from . import hotkey_tab as ht
    from . import llm_tab as lt
    from . import paste_tab as pat
    from . import pet_tab as petab

    global _window
    if _window is not None and _window.winfo_exists():
        _window.lift()
        _window.focus_set()
        return

    win = tk.Tk()
    win.title("agent-doctor — Preferences")
    win.geometry("520x440")

    notebook = ttk.Notebook(win)
    notebook.pack(fill="both", expand=True, padx=12, pady=12)

    _build_dictation_tab(notebook, dt)
    _build_llm_tab(notebook, lt)
    _build_hotkey_tab(notebook, ht)
    _build_paste_tab(notebook, pat)
    _build_pet_tab(notebook, petab)

    _window = win
    win.mainloop()


def _build_dictation_tab(notebook: Any, dt: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(notebook, padding=12)
    notebook.add(frame, text="Dictation")

    state = dt.DictationState.from_settings()
    options = dt.model_install_options()
    model_ids = [opt["id"] for opt in options]

    ttk.Label(frame, text="Whisper model:").grid(row=0, column=0, sticky="w", pady=4)
    model_var = tk.StringVar(value=state.model_id or "")
    model_dd = ttk.Combobox(frame, textvariable=model_var, values=model_ids, state="readonly", width=32)
    model_dd.grid(row=0, column=1, sticky="ew", pady=4)

    ttk.Label(frame, text="Language hint:").grid(row=1, column=0, sticky="w", pady=4)
    lang_var = tk.StringVar(value=state.language)
    ttk.Combobox(
        frame, textvariable=lang_var,
        values=["auto", "en", "zh", "ja", "es", "fr", "de"],
        state="readonly", width=10,
    ).grid(row=1, column=1, sticky="w", pady=4)

    ttk.Label(frame, text="Extra buffer (ms):").grid(row=2, column=0, sticky="w", pady=4)
    buf_var = tk.IntVar(value=state.extra_buffer_ms)
    ttk.Scale(
        frame, from_=0, to=500, orient="horizontal", variable=buf_var, length=240,
    ).grid(row=2, column=1, sticky="ew", pady=4)

    def commit(_event: Any = None) -> None:
        new_state = dt.DictationState(
            model_id=model_var.get() or None,
            model_path=next(
                (opt["path"] for opt in options if opt["id"] == model_var.get()),
                None,
            ),
            language=lang_var.get(),
            extra_buffer_ms=int(buf_var.get()),
        )
        try:
            new_state.apply()
            if new_state.model_id:
                dt.select_model(new_state.model_id)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))

    model_dd.bind("<<ComboboxSelected>>", commit)
    lang_var.trace_add("write", lambda *_: commit())
    buf_var.trace_add("write", lambda *_: commit())


def _build_llm_tab(notebook: Any, lt: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(notebook, padding=12)
    notebook.add(frame, text="LLM")

    state = lt.LLMState.from_settings()

    ttk.Label(frame, text="Provider:").grid(row=0, column=0, sticky="w", pady=4)
    prov_var = tk.StringVar(value=state.provider_id)
    ttk.Combobox(
        frame, textvariable=prov_var,
        values=["lm_studio", "ollama", "custom"], state="readonly", width=14,
    ).grid(row=0, column=1, sticky="w", pady=4)

    ttk.Label(frame, text="Base URL:").grid(row=1, column=0, sticky="w", pady=4)
    url_var = tk.StringVar(value=state.base_url)
    url_entry = ttk.Entry(frame, textvariable=url_var, width=42)
    url_entry.grid(row=1, column=1, sticky="ew", pady=4)

    ttk.Label(frame, text="Model:").grid(row=2, column=0, sticky="w", pady=4)
    model_var = tk.StringVar(value=state.model or "")
    model_entry = ttk.Entry(frame, textvariable=model_var, width=42)
    model_entry.grid(row=2, column=1, sticky="ew", pady=4)

    def commit(_event: Any = None) -> None:
        new_state = lt.LLMState(
            provider_id=prov_var.get(),
            base_url=url_var.get(),
            model=model_var.get() or None,
            api_key=None,
            timeout_s=state.timeout_s,
            optimize_prompt=state.optimize_prompt,
        )
        try:
            new_state.apply()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))

    prov_var.trace_add("write", lambda *_: commit())
    url_var.trace_add("write", lambda *_: commit())
    model_var.trace_add("write", lambda *_: commit())

    def test_connection() -> None:
        result = lt.probe_providers(timeout=3.0)
        row = next((r for r in result if r.provider_id == prov_var.get()), None)
        if row and row.reachable:
            messagebox.showinfo("Preferences", f"OK — {len(row.models)} model(s)")
        else:
            messagebox.showerror("Preferences", f"unreachable: {row.error if row else 'no row'}")

    ttk.Button(frame, text="Test connection", command=test_connection).grid(
        row=3, column=1, sticky="e", pady=8
    )

    def edit_prompt() -> None:
        dlg = tk.Toplevel(frame)
        dlg.title("Edit optimize prompt")
        dlg.geometry("560x360")
        text = tk.Text(dlg, wrap="word")
        text.insert("1.0", state.optimize_prompt or "")
        text.pack(fill="both", expand=True, padx=10, pady=10)

        def save_and_close() -> None:
            from agent_doctor import dictate_settings as ds_local
            s = ds_local.load()
            new = ds_local.LLMSettings(
                provider_id=s.llm.provider_id,
                base_url=s.llm.base_url,
                model=s.llm.model,
                api_key_ref=s.llm.api_key_ref,
                timeout_s=s.llm.timeout_s,
                optimize_prompt=text.get("1.0", "end-1c") or None,
            )
            ds_local.save(ds_local.replace_section(s, llm=new))
            dlg.destroy()

        ttk.Button(dlg, text="Save", command=save_and_close).pack(pady=6)

    ttk.Button(frame, text="Edit optimize prompt…", command=edit_prompt).grid(
        row=4, column=1, sticky="e", pady=4
    )


def _build_hotkey_tab(notebook: Any, ht: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(notebook, padding=12)
    notebook.add(frame, text="Hotkey")

    state = ht.HotkeyState.from_settings()

    ttk.Label(frame, text="Binding:").grid(row=0, column=0, sticky="w", pady=4)
    binding_var = tk.StringVar(value=state.binding)
    ttk.Entry(frame, textvariable=binding_var, width=32).grid(row=0, column=1, sticky="ew", pady=4)

    def record_chord() -> None:
        dlg = tk.Toplevel(frame)
        dlg.title("Record chord")
        dlg.geometry("320x140")
        tk.Label(dlg, text="Press the chord you want to use.").pack(pady=20)
        captured = tk.StringVar(value="(waiting…)")
        tk.Label(dlg, textvariable=captured, font=("Helvetica", 14, "bold")).pack()

        def on_key(event: Any) -> None:
            mods = []
            state_bits = event.state
            if state_bits & 0x10000:
                mods.append("cmd")
            if state_bits & 0x4:
                mods.append("ctrl")
            if state_bits & 0x8 or state_bits & 0x10:
                mods.append("option")
            if state_bits & 0x1:
                mods.append("shift")
            key = event.keysym.lower()
            if key in ("control_l", "control_r", "shift_l", "shift_r", "alt_l", "alt_r", "meta_l", "meta_r"):
                return
            mapping = {"escape": "escape", "return": "return", "tab": "tab", "space": "space"}
            tok = mapping.get(key, key)
            chord = "+".join(mods + [tok])
            captured.set(chord)
            binding_var.set(chord)
            dlg.after(400, dlg.destroy)

        dlg.bind("<Key>", on_key)
        dlg.focus_set()

    ttk.Button(frame, text="Record chord…", command=record_chord).grid(
        row=0, column=2, sticky="w", padx=6, pady=4
    )

    ttk.Label(frame, text="Mode:").grid(row=1, column=0, sticky="w", pady=4)
    ptt_var = tk.BooleanVar(value=state.push_to_talk)
    ttk.Radiobutton(frame, text="Push-to-talk", variable=ptt_var, value=True).grid(row=1, column=1, sticky="w")
    ttk.Radiobutton(frame, text="Toggle", variable=ptt_var, value=False).grid(row=2, column=1, sticky="w")

    def commit() -> None:
        try:
            ht.HotkeyState(binding=binding_var.get(), push_to_talk=bool(ptt_var.get())).apply()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))

    binding_var.trace_add("write", lambda *_: commit())
    ptt_var.trace_add("write", lambda *_: commit())

    def install() -> None:
        try:
            result = ht.install_daemon()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))
            return
        messagebox.showinfo("Preferences", f"Installed:\n{result}")

    def uninstall() -> None:
        ht.uninstall_daemon()
        messagebox.showinfo("Preferences", "Uninstalled.")

    ttk.Button(frame, text="Install daemon", command=install).grid(row=3, column=0, pady=12)
    ttk.Button(frame, text="Uninstall daemon", command=uninstall).grid(row=3, column=1, pady=12, sticky="w")


def _build_paste_tab(notebook: Any, pat: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(notebook, padding=12)
    notebook.add(frame, text="Paste")

    state = pat.PasteState.from_settings()

    auto_var = tk.BooleanVar(value=state.auto_paste)
    ttk.Checkbutton(frame, text="Auto-paste at cursor", variable=auto_var).grid(row=0, column=0, sticky="w", pady=4)

    ttk.Label(frame, text="Delay (ms):").grid(row=1, column=0, sticky="w", pady=4)
    delay_var = tk.IntVar(value=state.paste_delay_ms)
    ttk.Scale(frame, from_=0, to=250, orient="horizontal", variable=delay_var, length=240).grid(
        row=1, column=1, sticky="ew", pady=4
    )

    def commit() -> None:
        try:
            pat.PasteState(auto_paste=bool(auto_var.get()), paste_delay_ms=int(delay_var.get())).apply()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))
            from agent_doctor import dictate_settings as ds
            auto_var.set(ds.load().paste.auto_paste)

    auto_var.trace_add("write", lambda *_: commit())
    delay_var.trace_add("write", lambda *_: commit())

    def test() -> None:
        ok = pat.permission_test()
        messagebox.showinfo("Preferences", "OK" if ok else "FAILED — grant Accessibility permission")

    ttk.Button(frame, text="Run permission test", command=test).grid(row=2, column=1, sticky="e", pady=8)


def _build_pet_tab(notebook: Any, petab: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    frame = ttk.Frame(notebook, padding=12)
    notebook.add(frame, text="Pet")

    state = petab.PetUiState.from_settings()

    listening_var = tk.BooleanVar(value=state.animate_listening)
    thinking_var = tk.BooleanVar(value=state.animate_thinking)
    ttk.Checkbutton(frame, text="Animate during listening", variable=listening_var).grid(
        row=0, column=0, sticky="w", pady=4
    )
    ttk.Checkbutton(frame, text="Animate during thinking", variable=thinking_var).grid(
        row=1, column=0, sticky="w", pady=4
    )

    def commit() -> None:
        petab.PetUiState(
            animate_listening=bool(listening_var.get()),
            animate_thinking=bool(thinking_var.get()),
        ).apply()

    listening_var.trace_add("write", lambda *_: commit())
    thinking_var.trace_add("write", lambda *_: commit())

    def pick_sprite() -> None:
        path = filedialog.askopenfilename(title="Choose sprite (PNG)", filetypes=[("PNG", "*.png")])
        if not path:
            return
        try:
            from pathlib import Path
            petab.set_sprite_path(Path(path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))

    ttk.Button(frame, text="Choose custom sprite…", command=pick_sprite).grid(row=2, column=0, pady=8, sticky="w")
