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


@pytest.mark.parametrize(
    "keysym,expected",
    [
        ("Meta_L", "left_cmd"),
        ("Meta_R", "right_cmd"),
        ("Alt_L", "left_option"),
        ("Alt_R", "right_option"),
        ("Control_L", "left_ctrl"),
        ("Control_R", "right_ctrl"),
        ("Shift_L", "left_shift"),
        ("Shift_R", "right_shift"),
        ("Control", "ctrl"),
        ("Shift", "shift"),
        ("Alt", "option"),
        ("Meta", "cmd"),
        ("space", "space"),
        ("Return", "return"),
        ("Escape", "escape"),
        ("Tab", "tab"),
        ("BackSpace", "backspace"),
        ("Delete", "delete"),
        ("a", "a"),
        ("Z", "z"),
        ("5", "5"),
        ("F1", "f1"),
        ("F12", "f12"),
        ("Mode_switch", None),
        ("dead_grave", None),
    ],
)
def test_keysym_to_token(keysym: str, expected) -> None:
    from agent_doctor.ui.preferences.hotkey_tab_view import _keysym_to_token
    assert _keysym_to_token(keysym) == expected
