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
    win.geometry("640x560")
    win.resizable(True, True)

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

    from agent_doctor import dictate_settings as ds
    from agent_doctor import settings as gs
    from agent_doctor.gemini_image import NANO_BANANA_2_MODEL

    frame = ttk.Frame(notebook, padding=12)
    frame.columnconfigure(0, weight=1)
    notebook.add(frame, text="LLM")

    state = lt.LLMState.from_settings()

    # ---- Image generation section ----
    img_frame = ttk.LabelFrame(frame, text="Image generation (Gemini only)", padding=10)
    img_frame.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    img_frame.columnconfigure(1, weight=1)

    ttk.Label(img_frame, text="Provider:").grid(row=0, column=0, sticky="w", pady=2)
    ttk.Label(img_frame, text="Gemini (Nano Banana 2)").grid(row=0, column=1, sticky="w", pady=2)

    ttk.Label(img_frame, text="Model:").grid(row=1, column=0, sticky="w", pady=2)
    ttk.Label(img_frame, text=NANO_BANANA_2_MODEL).grid(row=1, column=1, sticky="w", pady=2)

    ttk.Label(img_frame, text="API key:").grid(row=2, column=0, sticky="w", pady=2)
    api_status_var = tk.StringVar()
    ttk.Label(img_frame, textvariable=api_status_var).grid(row=2, column=1, sticky="w", pady=2)

    btn_row = ttk.Frame(img_frame)
    btn_row.grid(row=3, column=1, sticky="w", pady=(4, 0))

    # ---- Transcription section ----
    txn_frame = ttk.LabelFrame(frame, text="Transcribed message handling", padding=10)
    txn_frame.grid(row=1, column=0, sticky="ew")
    txn_frame.columnconfigure(1, weight=1)

    ttk.Label(txn_frame, text="Provider:").grid(row=0, column=0, sticky="w", pady=4)
    prov_var = tk.StringVar(value=state.provider_id)
    ttk.Combobox(
        txn_frame,
        textvariable=prov_var,
        values=["lm_studio", "ollama", "custom", "gemini"],
        state="readonly",
        width=18,
    ).grid(row=0, column=1, sticky="w", pady=4)

    ttk.Label(txn_frame, text="Base URL:").grid(row=1, column=0, sticky="w", pady=4)
    url_var = tk.StringVar(value=state.base_url)
    ttk.Entry(txn_frame, textvariable=url_var).grid(row=1, column=1, sticky="ew", pady=4)

    ttk.Label(txn_frame, text="Model:").grid(row=2, column=0, sticky="w", pady=4)
    model_var = tk.StringVar(value=state.model or "")
    model_combo = ttk.Combobox(txn_frame, textvariable=model_var, values=())
    model_combo.grid(row=2, column=1, sticky="ew", pady=4)

    model_hint_var = tk.StringVar(value="")
    model_hint = ttk.Label(txn_frame, textvariable=model_hint_var, foreground="#888")
    model_hint.grid(row=3, column=1, sticky="w", pady=(0, 4))

    reuse_var = tk.BooleanVar(value=state.reuse_gemini_key)
    reuse_chk = ttk.Checkbutton(
        txn_frame,
        text="Reuse Gemini API key from image generation",
        variable=reuse_var,
    )
    reuse_chk.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 4))

    txn_btn_row = ttk.Frame(txn_frame)
    txn_btn_row.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ---- Helpers: refresh status / visibility ----

    def render_key_status() -> str:
        status = gs.settings_status()
        if not status.configured:
            return "not configured"
        if status.backend is gs.Backend.KEYRING:
            return "configured (Keychain)"
        if status.backend is gs.Backend.FILE:
            return "configured (file)"
        return "configured"

    def key_is_configured() -> bool:
        return gs.settings_status().configured

    def refresh_visibility(_event: Any = None) -> None:
        api_status_var.set(render_key_status())
        # Hide the reuse checkbox when provider == "gemini" (redundant), grey
        # it when provider != gemini AND no key is configured, otherwise show
        # and enable.
        if prov_var.get() == "gemini":
            reuse_chk.grid_remove()
        else:
            reuse_chk.grid()
            if key_is_configured():
                reuse_chk.state(["!disabled"])
            else:
                reuse_chk.state(["disabled"])
                reuse_var.set(False)

    # ---- Commit on edit (txn section) ----

    def commit_txn(_event: Any = None) -> None:
        new_state = lt.LLMState(
            provider_id=prov_var.get(),
            base_url=url_var.get(),
            model=model_var.get() or None,
            api_key=None,
            timeout_s=state.timeout_s,
            optimize_prompt=state.optimize_prompt,
            reuse_gemini_key=bool(reuse_var.get()),
        )
        try:
            new_state.apply()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preferences", str(exc))

    def refresh_model_choices() -> None:
        """Populate the Model combobox based on the current provider.

        For ``gemini``: synchronously probe Gemini's ``/models`` endpoint and
        list whatever ids come back. For everything else: empty list (the
        combobox degrades to a free-form entry). Also clears the model value
        when it no longer fits the new provider (a gemini id picked while on
        lm_studio, or a non-gemini id left over after switching to gemini).
        Surfaces upstream errors via ``model_hint_var`` so an empty dropdown
        explains itself instead of failing silently.
        """

        provider_id = prov_var.get()
        current = model_var.get().strip()
        if provider_id == "gemini":
            models, fetch_error = lt.gemini_models_status(timeout=3.0)
            model_combo.configure(values=tuple(models))
            if not lt.looks_like_gemini_model(current):
                model_var.set(models[0] if models else "")
            if fetch_error:
                model_hint_var.set(f"could not fetch Gemini models: {fetch_error}")
            else:
                model_hint_var.set("")
        else:
            model_combo.configure(values=())
            if lt.looks_like_gemini_model(current):
                model_var.set("")
            model_hint_var.set("")

    def on_provider_change(*_args: Any) -> None:
        from agent_doctor import dictate_llm as _dl
        try:
            p = _dl.get_provider(prov_var.get())
        except _dl.DictateLLMError:
            return
        if not p.allow_base_url_edit:
            url_var.set(p.base_url)
        refresh_visibility()
        refresh_model_choices()
        commit_txn()

    prov_var.trace_add("write", lambda *_: on_provider_change())
    url_var.trace_add("write", lambda *_: commit_txn())
    model_var.trace_add("write", lambda *_: commit_txn())
    reuse_var.trace_add("write", lambda *_: commit_txn())

    # ---- Image-section actions ----

    def open_set_key_dialog() -> None:
        dlg = tk.Toplevel(frame)
        dlg.title("Set Gemini API key")
        dlg.geometry("420x140")
        dlg.transient(frame.winfo_toplevel())
        dlg.grab_set()
        ttk.Label(dlg, text="Gemini API key:").pack(anchor="w", padx=12, pady=(12, 4))
        key_var = tk.StringVar()
        entry = ttk.Entry(dlg, textvariable=key_var, show="*", width=48)
        entry.pack(fill="x", padx=12)
        entry.focus_set()

        def save_and_close() -> None:
            value = key_var.get()
            if not value.strip():
                messagebox.showerror("Preferences", "API key cannot be empty.")
                return
            try:
                gs.store_gemini_key(value)
            except gs.SettingsError as exc:
                messagebox.showerror("Preferences", str(exc))
                return
            dlg.destroy()
            refresh_visibility()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=12)
        ttk.Button(btns, text="Save", command=save_and_close).pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(0, 6))
        dlg.bind("<Return>", lambda _e: save_and_close())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def clear_key() -> None:
        if not key_is_configured():
            return
        if not messagebox.askyesno(
            "Preferences",
            "Clear the stored Gemini API key from all backends?",
        ):
            return
        gs.clear_gemini_key()
        refresh_visibility()

    ttk.Button(btn_row, text="Set…", command=open_set_key_dialog).pack(side="left")
    ttk.Button(btn_row, text="Clear", command=clear_key).pack(side="left", padx=(6, 0))

    # ---- Txn-section actions ----

    def test_connection() -> None:
        row = lt.probe_one(prov_var.get(), timeout=3.0)
        if row.reachable:
            messagebox.showinfo("Preferences", f"OK — {len(row.models)} model(s)")
        else:
            messagebox.showerror("Preferences", f"unreachable: {row.error or 'no row'}")

    def edit_prompt() -> None:
        dlg = tk.Toplevel(frame)
        dlg.title("Edit optimize prompt")
        dlg.geometry("560x360")
        text = tk.Text(dlg, wrap="word")
        text.insert("1.0", state.optimize_prompt or "")
        text.pack(fill="both", expand=True, padx=10, pady=10)

        def save_and_close() -> None:
            s = ds.load()
            new = ds.LLMSettings(
                provider_id=s.llm.provider_id,
                base_url=s.llm.base_url,
                model=s.llm.model,
                api_key_ref=s.llm.api_key_ref,
                timeout_s=s.llm.timeout_s,
                optimize_prompt=text.get("1.0", "end-1c") or None,
                reuse_gemini_key=s.llm.reuse_gemini_key,
            )
            ds.save(ds.replace_section(s, llm=new))
            dlg.destroy()

        ttk.Button(dlg, text="Save", command=save_and_close).pack(pady=6)

    ttk.Button(txn_btn_row, text="Test connection", command=test_connection).pack(side="left")
    ttk.Button(txn_btn_row, text="Edit optimize prompt…", command=edit_prompt).pack(
        side="left", padx=(6, 0)
    )

    # Initial paint.
    refresh_visibility()
    refresh_model_choices()


def _build_hotkey_tab(notebook: Any, ht: Any) -> None:
    from . import hotkey_tab_view
    hotkey_tab_view.build(notebook)


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
