"""Smoke test: open + close the Preferences window under a real tk root."""

from __future__ import annotations

import pytest


@pytest.mark.tkinter
def test_open_window_does_not_crash(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import tkinter as tk

    from agent_doctor import dictate_settings as ds
    from agent_doctor.ui.preferences import open_window

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    ds.save(ds.default_settings())

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)
    open_window()
    root.destroy()


def test_hotkey_tab_view_imports_without_tk_root() -> None:
    # Import-only — instantiating widgets needs a Tk root which CI may lack.
    from agent_doctor.ui.preferences import hotkey_tab_view  # noqa: F401
