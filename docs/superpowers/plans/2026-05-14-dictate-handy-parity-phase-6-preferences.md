# Dictate Phase 6 — Preferences window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the new dictation knobs (model, language, LLM provider/URL/model + optimize prompt, hotkey binding + PTT, auto-paste, pet animations) under one tkinter Preferences window opened from the pet (right-click → "Preferences…") or via `agent-doctor dictate preferences`.

**Architecture:** UI lives under `agent_doctor/ui/preferences/`. Each tab is a separate file whose top-level class owns *settings logic only*; widget construction is a thin shim. The controller (`PreferencesController`) wires per-tab logic to a single `ttk.Notebook`. Read on focus, write on change. Daemon-affecting changes call `hotkey_install.sighup()`. CLI: `agent-doctor dictate preferences` opens the window; the pet menu adds a "Preferences…" item.

**Tech Stack:** Python stdlib + tkinter (already a dep of pet_display). No new packages.

**Spec:** `docs/superpowers/specs/2026-05-14-dictate-handy-parity-design.md` §10.

**Prereq:** Phases 1–5 landed.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_doctor/ui/__init__.py` | Create — namespace package marker. |
| `agent_doctor/ui/preferences/__init__.py` | Create — `PreferencesController`, `open_window()`. |
| `agent_doctor/ui/preferences/base.py` | Create — shared tab base + dirty/save helpers, headless-friendly. |
| `agent_doctor/ui/preferences/dictation_tab.py` | Create — Dictation tab (model, language, buffer). |
| `agent_doctor/ui/preferences/llm_tab.py` | Create — LLM tab (provider, url, model, optimize prompt). |
| `agent_doctor/ui/preferences/hotkey_tab.py` | Create — Hotkey tab (binding capture, PTT/toggle, install/uninstall). |
| `agent_doctor/ui/preferences/paste_tab.py` | Create — Paste tab (auto-paste toggle, delay, permission test). |
| `agent_doctor/ui/preferences/pet_tab.py` | Create — Pet tab (sprite, animation toggles). |
| `agent_doctor/cli.py` | Modify — register `agent-doctor dictate preferences`. |
| `agent_doctor/pet_display.py` | Modify — add "Preferences…" item to the right-click menu. |
| `tests/test_preferences_logic.py` | Create — controller unit tests (headless). |
| `tests/test_preferences_ui_smoke.py` | Create — open/close each tab under a real tk root, `@pytest.mark.tkinter`. |
| `tests/test_cli_subcommand_registration.py` | Modify — register `dictate preferences`. |

---

## Task 1: Tab base + dictation tab controller (logic only)

**Files:**
- Create: `agent_doctor/ui/__init__.py`, `agent_doctor/ui/preferences/__init__.py`, `agent_doctor/ui/preferences/base.py`, `agent_doctor/ui/preferences/dictation_tab.py`
- Test: `tests/test_preferences_logic.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_preferences_logic.py`:

```python
"""Headless tests for Preferences tab controllers (no tkinter)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_settings as ds
from agent_doctor.ui.preferences import dictation_tab as dt


def test_dictation_state_initialises_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-small",
            model_path=str(tmp_path / "ggml-small.bin"),
            language="en",
            extra_buffer_ms=222,
        ),
    )
    ds.save(settings)
    state = dt.DictationState.from_settings()
    assert state.model_id == "ggml-small"
    assert state.language == "en"
    assert state.extra_buffer_ms == 222


def test_dictation_state_apply_persists_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    state = dt.DictationState(
        model_id="ggml-medium",
        model_path=str(tmp_path / "ggml-medium.bin"),
        language="zh",
        extra_buffer_ms=100,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.transcription.model_id == "ggml-medium"
    assert loaded.transcription.language == "zh"
    assert loaded.transcription.extra_buffer_ms == 100


def test_dictation_state_validates_buffer_range() -> None:
    with pytest.raises(dt.DictationStateError, match="buffer"):
        dt.DictationState(
            model_id=None,
            model_path=None,
            language="auto",
            extra_buffer_ms=-1,
        ).apply()


def test_install_options_lists_catalog_with_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_models as dm

    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    options = dt.model_install_options()
    ids = {opt["id"] for opt in options}
    assert "ggml-large-v3-turbo" in ids
    for opt in options:
        assert "installed" in opt
        assert "display_name" in opt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_preferences_logic.py -v`
Expected: ImportError on `agent_doctor.ui.preferences.dictation_tab`.

- [ ] **Step 3: Create the package and dictation tab logic**

Create `agent_doctor/ui/__init__.py`:

```python
"""Optional UI surfaces for agent-doctor.

Submodules are tkinter-based and lazy-imported so the headless CLI path stays
dependency-free.
"""
```

Create `agent_doctor/ui/preferences/__init__.py`:

```python
"""Preferences window root."""

from .base import TabController  # re-export
```

Create `agent_doctor/ui/preferences/base.py`:

```python
"""Shared base class for Preferences tab controllers.

Tabs split UI from logic explicitly so the logic layer can be unit-tested
without tkinter. The base class is intentionally tiny — tabs override
``from_settings()`` and ``apply()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TabController(ABC):
    @classmethod
    @abstractmethod
    def from_settings(cls) -> "TabController":
        """Load the current persisted settings into a fresh controller."""

    @abstractmethod
    def apply(self) -> None:
        """Persist the controller's current state back to settings."""
```

Create `agent_doctor/ui/preferences/dictation_tab.py`:

```python
"""Dictation tab logic (model, language, buffer)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from agent_doctor import dictate_models as dm
from agent_doctor import dictate_settings as ds


class DictationStateError(ValueError):
    pass


@dataclass
class DictationState:
    model_id: Optional[str]
    model_path: Optional[str]
    language: str
    extra_buffer_ms: int

    @classmethod
    def from_settings(cls) -> "DictationState":
        s = ds.load()
        return cls(
            model_id=s.transcription.model_id,
            model_path=s.transcription.model_path,
            language=s.transcription.language,
            extra_buffer_ms=s.transcription.extra_buffer_ms,
        )

    def apply(self) -> None:
        if self.extra_buffer_ms < 0 or self.extra_buffer_ms > 500:
            raise DictationStateError(
                f"extra_buffer_ms must be 0..500 (got {self.extra_buffer_ms})"
            )
        s = ds.load()
        new = ds.TranscriptionSettings(
            model_id=self.model_id,
            model_path=self.model_path,
            language=self.language,
            extra_buffer_ms=int(self.extra_buffer_ms),
        )
        ds.save(ds.replace_section(s, transcription=new))


def model_install_options() -> List[dict]:
    """Return one row per catalog entry, augmented with install status."""

    return dm.list_status()


def select_model(model_id: str) -> None:
    """Make ``model_id`` the active transcription model. Caller must ensure it
    is installed."""

    dm.set_active(model_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_preferences_logic.py -v`
Expected: 4 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui tests/test_preferences_logic.py
git commit -m "feat(ui): scaffold preferences package + dictation tab logic"
```

---

## Task 2: LLM tab logic + tests

**Files:**
- Create: `agent_doctor/ui/preferences/llm_tab.py`
- Test: `tests/test_preferences_logic.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preferences_logic.py`:

```python
from agent_doctor.ui.preferences import llm_tab as lt


def test_llm_state_from_and_to_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    state = lt.LLMState(
        provider_id="ollama",
        base_url="http://localhost:11434/v1",
        model="llama3.1:8b",
        api_key=None,
        timeout_s=20,
        optimize_prompt=None,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.llm.provider_id == "ollama"
    assert loaded.llm.base_url == "http://localhost:11434/v1"
    assert loaded.llm.model == "llama3.1:8b"
    assert loaded.llm.timeout_s == 20


def test_llm_state_blocks_custom_base_url_on_non_custom_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    with pytest.raises(lt.LLMStateError, match="custom"):
        lt.LLMState(
            provider_id="lm_studio",
            base_url="http://elsewhere/v1",
            model=None,
            api_key=None,
            timeout_s=30,
            optimize_prompt=None,
        ).apply()


def test_llm_state_probe_returns_rows() -> None:
    """The tab uses ``probe_all`` so we just sanity-check the bridge."""

    rows = lt.probe_providers(timeout=0.5)
    ids = {r.provider_id for r in rows}
    assert ids == {"lm_studio", "ollama", "custom"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_preferences_logic.py -v`
Expected: 3 failing (ImportError + missing class).

- [ ] **Step 3: Implement the LLM tab**

Create `agent_doctor/ui/preferences/llm_tab.py`:

```python
"""LLM tab logic (provider, base_url, model, optimize prompt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from agent_doctor import dictate_llm as dl
from agent_doctor import dictate_settings as ds


class LLMStateError(ValueError):
    pass


@dataclass
class LLMState:
    provider_id: str
    base_url: str
    model: Optional[str]
    api_key: Optional[str]
    timeout_s: int
    optimize_prompt: Optional[str]

    @classmethod
    def from_settings(cls) -> "LLMState":
        s = ds.load()
        return cls(
            provider_id=s.llm.provider_id,
            base_url=s.llm.base_url,
            model=s.llm.model,
            api_key=None,
            timeout_s=s.llm.timeout_s,
            optimize_prompt=s.llm.optimize_prompt,
        )

    def apply(self) -> None:
        provider = dl.get_provider(self.provider_id)
        if not provider.allow_base_url_edit and self.base_url != provider.base_url:
            raise LLMStateError(
                f"provider {self.provider_id!r} requires base_url {provider.base_url!r}; "
                "switch to 'custom' to override"
            )
        if self.timeout_s <= 0 or self.timeout_s > 600:
            raise LLMStateError(f"timeout_s must be 1..600 (got {self.timeout_s})")
        s = ds.load()
        new = ds.LLMSettings(
            provider_id=self.provider_id,
            base_url=self.base_url,
            model=self.model,
            api_key_ref=s.llm.api_key_ref,
            timeout_s=int(self.timeout_s),
            optimize_prompt=self.optimize_prompt,
        )
        ds.save(ds.replace_section(s, llm=new))


def probe_providers(timeout: float = 5.0) -> List[dl.ProbeResult]:
    return dl.probe_all(timeout=timeout)


def fetch_models_for(provider_id: str, base_url: Optional[str] = None, *, timeout: float = 5.0):
    p = dl.get_provider(provider_id)
    url = base_url or p.base_url
    return dl.probe(url, p.models_endpoint, timeout=timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_preferences_logic.py -v`
Expected: 7 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/llm_tab.py tests/test_preferences_logic.py
git commit -m "feat(ui): LLM tab logic with provider validation"
```

---

## Task 3: Hotkey + Paste + Pet tab logic

**Files:**
- Create: `agent_doctor/ui/preferences/hotkey_tab.py`, `paste_tab.py`, `pet_tab.py`
- Test: `tests/test_preferences_logic.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preferences_logic.py`:

```python
from agent_doctor.ui.preferences import hotkey_tab as ht
from agent_doctor.ui.preferences import paste_tab as pat
from agent_doctor.ui.preferences import pet_tab as petab


def test_hotkey_state_apply_persists_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    ht.HotkeyState(binding="ctrl+option+space", push_to_talk=False).apply()
    s = ds.load()
    assert s.hotkey.binding == "ctrl+option+space"
    assert s.hotkey.push_to_talk is False

    with pytest.raises(ht.HotkeyStateError, match="conflict"):
        ht.HotkeyState(binding="cmd+space", push_to_talk=True).apply()


def test_paste_state_disable_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    pat.PasteState(auto_paste=False, paste_delay_ms=80).apply()
    s = ds.load()
    assert s.paste.auto_paste is False
    assert s.paste.paste_delay_ms == 80


def test_paste_state_enable_requires_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(
        "agent_doctor.dictate_paste._default_osascript",
        lambda _argv: 1,  # simulate permission denied
    )
    monkeypatch.setattr(
        "agent_doctor.dictate_paste._default_pbcopy",
        lambda _argv, _data: 0,
    )
    with pytest.raises(pat.PasteStateError, match="permission"):
        pat.PasteState(auto_paste=True, paste_delay_ms=60).apply()


def test_pet_state_toggle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    petab.PetUiState(animate_listening=False, animate_thinking=True).apply()
    s = ds.load()
    assert s.pet.animate_listening is False
    assert s.pet.animate_thinking is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_preferences_logic.py -v`
Expected: 4 new failures.

- [ ] **Step 3: Implement hotkey_tab.py**

Create `agent_doctor/ui/preferences/hotkey_tab.py`:

```python
"""Hotkey tab logic (chord capture, PTT/toggle, daemon kickstart)."""

from __future__ import annotations

from dataclasses import dataclass

from agent_doctor import dictate_settings as ds
from agent_doctor import hotkey_install as hi
from agent_doctor import hotkey_parse as hp


class HotkeyStateError(ValueError):
    pass


@dataclass
class HotkeyState:
    binding: str
    push_to_talk: bool

    @classmethod
    def from_settings(cls) -> "HotkeyState":
        s = ds.load()
        return cls(binding=s.hotkey.binding, push_to_talk=s.hotkey.push_to_talk)

    def apply(self) -> None:
        try:
            chord = hp.parse(self.binding)
        except hp.HotkeyParseError as exc:
            raise HotkeyStateError(str(exc)) from exc
        s = ds.load()
        new = ds.HotkeySettings(
            binding=chord.canonical(),
            push_to_talk=bool(self.push_to_talk),
            daemon_enabled=s.hotkey.daemon_enabled,
        )
        ds.save(ds.replace_section(s, hotkey=new))
        if hi.DEFAULT_PLIST_PATH.exists():
            hi.sighup()


def install_daemon() -> dict[str, str]:
    return hi.install()


def uninstall_daemon() -> dict[str, str]:
    return hi.uninstall()


def daemon_status() -> dict[str, object]:
    return hi.status()
```

- [ ] **Step 4: Implement paste_tab.py**

Create `agent_doctor/ui/preferences/paste_tab.py`:

```python
"""Paste tab logic (auto-paste toggle, paste delay, permission test)."""

from __future__ import annotations

from dataclasses import dataclass

from agent_doctor import dictate_paste as dp
from agent_doctor import dictate_settings as ds


class PasteStateError(ValueError):
    pass


@dataclass
class PasteState:
    auto_paste: bool
    paste_delay_ms: int

    @classmethod
    def from_settings(cls) -> "PasteState":
        s = ds.load()
        return cls(auto_paste=s.paste.auto_paste, paste_delay_ms=s.paste.paste_delay_ms)

    def apply(self) -> None:
        if self.paste_delay_ms < 0 or self.paste_delay_ms > 250:
            raise PasteStateError(
                f"paste_delay_ms must be 0..250 (got {self.paste_delay_ms})"
            )
        if self.auto_paste:
            try:
                dp.enable()
            except dp.PasteError as exc:
                raise PasteStateError(str(exc)) from exc
        else:
            dp.disable()
        s = ds.load()
        new = ds.PasteSettings(
            auto_paste=self.auto_paste,
            paste_delay_ms=int(self.paste_delay_ms),
            last_permission_check=s.paste.last_permission_check,
        )
        ds.save(ds.replace_section(s, paste=new))


def permission_test() -> bool:
    return dp.permission_test()
```

- [ ] **Step 5: Implement pet_tab.py**

Create `agent_doctor/ui/preferences/pet_tab.py`:

```python
"""Pet tab logic (animation toggles + sprite picker bridge)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from agent_doctor import dictate_settings as ds


class PetUiStateError(ValueError):
    pass


@dataclass
class PetUiState:
    animate_listening: bool
    animate_thinking: bool

    @classmethod
    def from_settings(cls) -> "PetUiState":
        s = ds.load()
        return cls(
            animate_listening=s.pet.animate_listening,
            animate_thinking=s.pet.animate_thinking,
        )

    def apply(self) -> None:
        s = ds.load()
        new = ds.PetSettings(
            animate_listening=bool(self.animate_listening),
            animate_thinking=bool(self.animate_thinking),
        )
        ds.save(ds.replace_section(s, pet=new))


def set_sprite_path(source: Path) -> Path:
    """Copy ``source`` to the user sprite path; returns the destination."""

    from agent_doctor.pet_display import user_sprite_path

    dest = user_sprite_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise PetUiStateError(f"sprite not found: {source}")
    shutil.copyfile(source, dest)
    return dest
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_preferences_logic.py -v`
Expected: all green (11 tests).

- [ ] **Step 7: Commit**

```bash
git add agent_doctor/ui/preferences/hotkey_tab.py \
        agent_doctor/ui/preferences/paste_tab.py \
        agent_doctor/ui/preferences/pet_tab.py \
        tests/test_preferences_logic.py
git commit -m "feat(ui): hotkey/paste/pet tab logic with validation"
```

---

## Task 4: Tkinter window + CLI entry point

**Files:**
- Modify: `agent_doctor/ui/preferences/__init__.py`, `agent_doctor/cli.py`
- Test: `tests/test_preferences_ui_smoke.py`

- [ ] **Step 1: Add CLI registration**

In `agent_doctor/cli.py`, after the `dictate paste` block from Phase 5 Task 3, append:

```python
    dictate_prefs = dictate_subs.add_parser(
        "preferences",
        help="Open the Preferences window (tkinter).",
    )
    dictate_prefs.set_defaults(func=_cmd_dictate_preferences)
```

Add the handler:

```python
def _cmd_dictate_preferences(_args: argparse.Namespace) -> int:
    try:
        from agent_doctor.ui.preferences import open_window
    except Exception as exc:  # noqa: BLE001 - tkinter missing on Linux CI is OK
        print(f"agent-doctor: preferences UI unavailable: {exc}", file=sys.stderr)
        return 2
    open_window()
    return 0
```

- [ ] **Step 2: Implement `open_window()`**

Replace `agent_doctor/ui/preferences/__init__.py` with:

```python
"""Preferences window root.

Opens a singleton tkinter Toplevel with five tabs. The widget code lives here
because it's small and shared across tabs; per-tab *logic* (validation,
settings I/O) lives in the sibling tab modules so it can be unit-tested headless.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import TabController  # re-export


def open_window() -> None:
    """Open the Preferences window. Singleton — second invocation raises focus."""

    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    from . import dictation_tab as dt
    from . import hotkey_tab as ht
    from . import llm_tab as lt
    from . import paste_tab as pat
    from . import pet_tab as petab

    # Singleton via a module-level reference.
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


_window: Optional[Any] = None


def _build_dictation_tab(notebook: Any, dt: Any) -> None:
    import tkinter as tk
    from tkinter import ttk

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
            from tkinter import messagebox
            messagebox.showerror("Preferences", str(exc))

    model_dd.bind("<<ComboboxSelected>>", commit)
    lang_var.trace_add("write", lambda *_: commit())
    buf_var.trace_add("write", lambda *_: commit())


def _build_llm_tab(notebook: Any, lt: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog

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
        # Custom multi-line dialog because tkinter.simpledialog has no native
        # multi-line equivalent.
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
            # tkinter's event.state bitmask is annoying; use keysym + state heuristics.
            state_bits = event.state
            if state_bits & 0x10000:  # Cmd (varies; macOS reports 0x10000)
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
            # Map tk keysyms back to our token vocabulary.
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
            # Re-sync the checkbox to the persisted value in case enable failed.
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
```

- [ ] **Step 3: Add the smoke test**

Create `tests/test_preferences_ui_smoke.py`:

```python
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
    # Patch mainloop so we don't block.
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)
    open_window()
    root.destroy()
```

- [ ] **Step 4: Run the headless suite**

Run: `python3 -m pytest -q -m "not tkinter"`
Expected: green.

- [ ] **Step 5: Run the tkinter smoke locally (optional)**

Run: `python3 -m pytest -m tkinter -v`
Expected: green or skipped on CI; on a Mac/Linux desktop the window opens briefly.

- [ ] **Step 6: Commit**

```bash
git add agent_doctor/ui/preferences/__init__.py agent_doctor/cli.py \
        tests/test_preferences_ui_smoke.py
git commit -m "feat(ui): Preferences window (5 tabs) + 'dictate preferences' CLI"
```

---

## Task 5: Pet right-click → "Preferences…"

**Files:**
- Modify: `agent_doctor/pet_display.py`

- [ ] **Step 1: Add the menu item**

In `agent_doctor/pet_display.py`, locate the right-click handler. The current pet window builds a context menu in `display_pet` (search for `Menu(` or `tk.Menu`; if no context menu exists yet, we add one). After existing menu items, add a "Preferences…" entry that invokes `open_window()` in a daemon thread to keep the pet responsive:

```python
def _open_preferences() -> None:
    import threading
    from agent_doctor.ui.preferences import open_window
    threading.Thread(target=open_window, daemon=True).start()
```

Then in the right-click menu construction:

```python
menu.add_command(label="Preferences…", command=_open_preferences)
```

If no menu currently exists, create one bound to `<Button-2>` / `<Button-3>`:

```python
menu = tk.Menu(root, tearoff=0)
menu.add_command(label="Preferences…", command=_open_preferences)
menu.add_separator()
menu.add_command(label="Quit", command=root.destroy)

def _show_menu(event: Any) -> None:
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()

canvas.bind("<Button-2>", _show_menu)
canvas.bind("<Button-3>", _show_menu)
canvas.bind("<Control-Button-1>", _show_menu)
```

- [ ] **Step 2: Manual smoke**

```bash
python3 -m agent_doctor.cli pet-display &
# Right-click the pet -> "Preferences…" -> the window opens.
```

- [ ] **Step 3: Commit**

```bash
git add agent_doctor/pet_display.py
git commit -m "feat(pet): right-click menu adds Preferences… entry"
```

---

## Task 6: README + registration smoke + final run

**Files:**
- Modify: `README.md`, `tests/test_cli_subcommand_registration.py`

- [ ] **Step 1: Update README**

```markdown
### Preferences window

Open the GUI:

```bash
agent-doctor dictate preferences
```

or right-click the pet sprite and choose "Preferences…". Five tabs:

- **Dictation** — model, language, extra recording buffer.
- **LLM** — provider (LM Studio / Ollama / Custom), base URL, model, test connection.
- **Hotkey** — chord, push-to-talk vs toggle, install / uninstall daemon.
- **Paste** — auto-paste toggle, delay, permission test.
- **Pet** — listening / thinking animation toggles, sprite picker.

All changes save immediately. No Save button.
```

- [ ] **Step 2: Register the new CLI command**

Append to `tests/test_cli_subcommand_registration.py`:

```python
def test_dictate_preferences_subcommand_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    assert "preferences" in _subparser_choices(dictate_sub)
```

- [ ] **Step 3: Final run**

Run: `python3 -m pytest -q -m "not tkinter"`
Expected: green.

- [ ] **Step 4: Commit + tag**

```bash
git add README.md tests/test_cli_subcommand_registration.py
git commit -m "docs(prefs): document the Preferences window"
git tag dictate-phase-6-complete
```

---

## Phase 6 verification checklist

- [ ] `python3 -m pytest -q -m "not tkinter"` is green.
- [ ] `python3 -m pytest -m tkinter -v` is green on a desktop machine (skipped on headless CI).
- [ ] `agent-doctor dictate preferences` opens a 5-tab window without crashing.
- [ ] Changing any control persists to `~/.agent-doctor/dictate.json` immediately (verify with `cat`).
- [ ] Right-clicking the pet shows the Preferences… menu item.
- [ ] Auto-paste enable in the Paste tab fails gracefully (without flipping the setting) when Accessibility is missing.
- [ ] Hotkey tab Install / Uninstall daemon works end-to-end on a swiftc-equipped machine.
- [ ] No new runtime dependencies introduced.
