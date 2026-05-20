# LLM Tab Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Preferences > LLM tab cutoff and split it into two LabelFrames — one for Gemini-only image generation (pet sprite), one for the existing transcribed-message LLM with optional Gemini key reuse.

**Architecture:** Four small, layered edits — (1) `dictate_settings.LLMSettings` gains one bool `reuse_gemini_key`; (2) `dictate_llm` adds `gemini` as a 4th provider and extends `llm_config()` key resolution to fall back to `settings.load_gemini_key()`; (3) `ui.preferences.llm_tab` threads the new field through `LLMState`; (4) `ui.preferences.__init__._build_llm_tab` is rewritten to render two `ttk.LabelFrame` sections, with the Gemini section invoking the existing `agent_doctor.settings` API for set/clear. No schema-version bump (new bool defaults to false, read-compatible with legacy JSON).

**Tech Stack:** Python 3.11+, pytest, ttk/tkinter, existing macOS Keychain backend via `agent_doctor.settings`.

**Spec:** `docs/superpowers/specs/2026-05-20-llm-tab-restructure-design.md` — read it before starting.

---

## File Structure

**Modify:**

- `agent_doctor/dictate_llm.py` — append `gemini` provider to `_PROVIDERS`; extend `llm_config()` to fall back to `settings.load_gemini_key()` when `provider_id == "gemini"` or `reuse_gemini_key` is True.
- `agent_doctor/dictate_settings.py` — add `reuse_gemini_key: bool = False` to `LLMSettings`; update `_to_dict` / `_from_dict`.
- `agent_doctor/ui/preferences/llm_tab.py` — add `reuse_gemini_key` to `LLMState` (round-trip + apply); add a helper `resolve_test_connection_key()` returning the key to pass to `dictate_llm.probe()`.
- `agent_doctor/ui/preferences/__init__.py` — bump window geometry from `520x440` to `640x560`; rewrite `_build_llm_tab` for the two-LabelFrame layout, including the Gemini key Set/Clear modal and the reuse checkbox visibility rules.
- `tests/test_preferences_logic.py` — add eight headless logic tests.

**No changes to:** `agent_doctor/gemini_image.py`, `agent_doctor/cli.py`, `agent_doctor/settings.py`.

---

## Tasks

### Task 1: Add `reuse_gemini_key` to LLMSettings

**Files:**
- Modify: `agent_doctor/dictate_settings.py:44-51` (dataclass), `:99-106` (`_to_dict`), `:155-162` (`_from_dict`)
- Test: `tests/test_preferences_logic.py` (new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preferences_logic.py`:

```python
def test_llm_settings_reuse_gemini_key_default_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default value must be False so legacy dictate.json files load unchanged."""
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    s = ds.default_settings()
    assert s.llm.reuse_gemini_key is False


def test_llm_settings_reuse_gemini_key_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    s = ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(reuse_gemini_key=True),
    )
    ds.save(s)
    loaded = ds.load()
    assert loaded.llm.reuse_gemini_key is True


def test_llm_settings_legacy_json_without_field_defaults_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing dictate.json with no reuse_gemini_key key must load cleanly."""
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    import json as _json
    legacy_payload = {
        "version": 1,
        "llm": {
            "provider_id": "lm_studio",
            "base_url": "http://localhost:1234/v1",
            "model": "qwen3",
            "api_key_ref": None,
            "timeout_s": 30,
            "optimize_prompt": None,
        },
    }
    (tmp_path / "dictate.json").write_text(_json.dumps(legacy_payload), encoding="utf-8")
    loaded = ds.load()
    assert loaded.llm.reuse_gemini_key is False
    assert loaded.llm.provider_id == "lm_studio"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_preferences_logic.py::test_llm_settings_reuse_gemini_key_default_false tests/test_preferences_logic.py::test_llm_settings_reuse_gemini_key_round_trip tests/test_preferences_logic.py::test_llm_settings_legacy_json_without_field_defaults_false -v`

Expected: FAIL — `LLMSettings.__init__() got an unexpected keyword argument 'reuse_gemini_key'` and/or `AttributeError: 'LLMSettings' object has no attribute 'reuse_gemini_key'`.

- [ ] **Step 3: Add the field to the dataclass**

In `agent_doctor/dictate_settings.py`, replace the `LLMSettings` dataclass (currently lines 44-51) with:

```python
@dataclass(frozen=True)
class LLMSettings:
    provider_id: str = "lm_studio"
    base_url: str = "http://localhost:1234/v1"
    model: Optional[str] = None
    api_key_ref: Optional[str] = None
    timeout_s: int = 30
    optimize_prompt: Optional[str] = None
    reuse_gemini_key: bool = False
```

- [ ] **Step 4: Update `_to_dict`**

In `agent_doctor/dictate_settings.py`, in the `_to_dict` function, replace the `"llm"` block (currently lines 99-106) with:

```python
        "llm": {
            "provider_id": settings.llm.provider_id,
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
            "api_key_ref": settings.llm.api_key_ref,
            "timeout_s": settings.llm.timeout_s,
            "optimize_prompt": settings.llm.optimize_prompt,
            "reuse_gemini_key": settings.llm.reuse_gemini_key,
        },
```

- [ ] **Step 5: Update `_from_dict`**

In `agent_doctor/dictate_settings.py`, in the `_from_dict` function, replace the `llm=LLMSettings(...)` block (currently lines 155-162) with:

```python
        llm=LLMSettings(
            provider_id=llm_d.get("provider_id", "lm_studio"),
            base_url=llm_d.get("base_url", "http://localhost:1234/v1"),
            model=llm_d.get("model"),
            api_key_ref=llm_d.get("api_key_ref"),
            timeout_s=timeout_s,
            optimize_prompt=llm_d.get("optimize_prompt"),
            reuse_gemini_key=bool(llm_d.get("reuse_gemini_key", False)),
        ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_preferences_logic.py -v -k "reuse_gemini_key or legacy_json"`

Expected: PASS for the three new tests. Existing tests must continue passing — run `pytest tests/test_preferences_logic.py -v` to confirm.

- [ ] **Step 7: Commit**

```bash
git add agent_doctor/dictate_settings.py tests/test_preferences_logic.py
git commit -m "feat(settings): add reuse_gemini_key field to LLMSettings"
```

---

### Task 2: Add `gemini` provider to dictate_llm catalog

**Files:**
- Modify: `agent_doctor/dictate_llm.py:48-73` (the `_PROVIDERS` tuple)
- Test: `tests/test_preferences_logic.py` (new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preferences_logic.py`:

```python
def test_dictate_llm_providers_includes_gemini() -> None:
    from agent_doctor import dictate_llm as dl
    ids = {p.id for p in dl.providers()}
    assert ids == {"lm_studio", "ollama", "custom", "gemini"}


def test_dictate_llm_gemini_provider_shape() -> None:
    from agent_doctor import dictate_llm as dl
    p = dl.get_provider("gemini")
    assert p.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert p.models_endpoint == "/models"
    assert p.requires_api_key is True
    assert p.allow_base_url_edit is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_preferences_logic.py::test_dictate_llm_providers_includes_gemini tests/test_preferences_logic.py::test_dictate_llm_gemini_provider_shape -v`

Expected: FAIL — assertion error on the set comparison, or `DictateLLMError: unknown provider 'gemini'`.

- [ ] **Step 3: Append the Gemini provider**

In `agent_doctor/dictate_llm.py`, replace the `_PROVIDERS` tuple (currently lines 48-73) with:

```python
_PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="lm_studio",
        label="LM Studio (local)",
        base_url="http://localhost:1234/v1",
        models_endpoint="/models",
        requires_api_key=False,
        allow_base_url_edit=False,
    ),
    Provider(
        id="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        models_endpoint="/models",
        requires_api_key=False,
        allow_base_url_edit=False,
    ),
    Provider(
        id="custom",
        label="Custom (OpenAI-compatible)",
        base_url="http://localhost:8080/v1",
        models_endpoint="/models",
        requires_api_key=False,
        allow_base_url_edit=True,
    ),
    Provider(
        id="gemini",
        label="Gemini (OpenAI-compatible)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models_endpoint="/models",
        requires_api_key=True,
        allow_base_url_edit=False,
    ),
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_preferences_logic.py::test_dictate_llm_providers_includes_gemini tests/test_preferences_logic.py::test_dictate_llm_gemini_provider_shape -v`

Expected: PASS.

Run the full file too: `pytest tests/test_preferences_logic.py -v`

Expected: all pass (the existing `test_llm_state_probe_returns_rows` test asserts the set is exactly `{lm_studio, ollama, custom}` — read it carefully before this task; if it's strict, update it in this task too).

- [ ] **Step 5: Update the legacy probe-set test if needed**

If `test_llm_state_probe_returns_rows` failed in step 4, replace its body so the assertion accepts the new set:

```python
def test_llm_state_probe_returns_rows() -> None:
    """The tab uses ``probe_all`` so we just sanity-check the bridge."""

    rows = lt.probe_providers(timeout=0.5)
    ids = {r.provider_id for r in rows}
    assert ids == {"lm_studio", "ollama", "custom", "gemini"}
```

Re-run: `pytest tests/test_preferences_logic.py -v`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add agent_doctor/dictate_llm.py tests/test_preferences_logic.py
git commit -m "feat(dictate_llm): add gemini as a 4th OpenAI-compatible provider"
```

---

### Task 3: Extend `llm_config()` to resolve Gemini key

**Files:**
- Modify: `agent_doctor/dictate_llm.py:191-231` (the `llm_config` function)
- Test: `tests/test_preferences_logic.py` (new tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preferences_logic.py`:

```python
def test_llm_config_uses_gemini_key_when_provider_is_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_llm as dl
    from agent_doctor import settings as gs

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(gs, "load_gemini_key", lambda: "gemini-key-A")

    ds.save(ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(
            provider_id="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-2.5-flash",
            reuse_gemini_key=False,
        ),
    ))

    cfg = dl.llm_config()
    assert cfg.api_key == "gemini-key-A"


def test_llm_config_uses_gemini_key_when_reuse_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_llm as dl
    from agent_doctor import settings as gs

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(gs, "load_gemini_key", lambda: "gemini-key-B")

    ds.save(ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(
            provider_id="custom",
            base_url="http://localhost:8080/v1",
            model="qwen3",
            reuse_gemini_key=True,
        ),
    ))

    cfg = dl.llm_config()
    assert cfg.api_key == "gemini-key-B"


def test_llm_config_explicit_kwarg_beats_gemini_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_llm as dl
    from agent_doctor import settings as gs

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(gs, "load_gemini_key", lambda: "should-be-ignored")

    ds.save(ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(provider_id="gemini", reuse_gemini_key=True),
    ))

    cfg = dl.llm_config(api_key="explicit-kwarg")
    assert cfg.api_key == "explicit-kwarg"


def test_llm_config_returns_none_key_when_neither_provider_nor_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: non-gemini provider + reuse=False must NOT pick up the gemini key."""
    from agent_doctor import dictate_llm as dl
    from agent_doctor import settings as gs

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(gs, "load_gemini_key", lambda: "must-not-leak")

    ds.save(ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(provider_id="lm_studio", reuse_gemini_key=False),
    ))

    cfg = dl.llm_config()
    assert cfg.api_key is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_preferences_logic.py -v -k "llm_config"`

Expected: FAIL — `cfg.api_key` is `None` for the first three (no reuse logic yet); the fourth may pass by accident, that's fine.

- [ ] **Step 3: Extend `llm_config()`**

In `agent_doctor/dictate_llm.py`, replace the `llm_config` function (currently lines 191-231) with:

```python
def llm_config(
    *,
    url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMConfig:
    """Resolve an :class:`LLMConfig` with this precedence order:

    1. Explicit kwargs (CLI flags)
    2. Environment variables
    3. ``~/.agent-doctor/dictate.json`` settings
    4. Provider-default base URL
    5. (api_key only) If provider is ``gemini`` OR ``reuse_gemini_key`` is set,
       fall back to ``agent_doctor.settings.load_gemini_key()``.

    The settings ``base_url`` is OpenAI-style root (``.../v1``) - we append
    ``/chat/completions`` so the existing ``_default_llm_call`` continues to
    work. The legacy env var keeps its historical literal value so users with
    pre-existing config are unaffected.
    """

    from . import dictate_settings as _ds  # local import to avoid cycle

    settings = _ds.load()
    provider = get_provider(settings.llm.provider_id)
    settings_url = (
        (settings.llm.base_url or provider.base_url).rstrip("/") + "/chat/completions"
    )
    settings_model = settings.llm.model

    resolved_url = url or os.environ.get(ENV_LLM_URL) or settings_url
    resolved_model = (
        model or os.environ.get(ENV_LLM_MODEL) or settings_model or "default"
    )

    # api_key precedence: explicit kwarg > env > gemini-reuse fallback > None.
    # The gemini-reuse fallback fires when the user picked the gemini provider
    # (its endpoint requires a key) or ticked the "reuse Gemini API key"
    # checkbox while on another provider. Local import keeps the
    # dictate_llm <-> settings module pair acyclic.
    resolved_key: Optional[str] = api_key or os.environ.get(ENV_LLM_KEY)
    if resolved_key is None and (
        settings.llm.provider_id == "gemini" or settings.llm.reuse_gemini_key
    ):
        from . import settings as _gs
        resolved_key = _gs.load_gemini_key()

    return LLMConfig(
        url=resolved_url,
        model=resolved_model,
        api_key=resolved_key,
        timeout=float(settings.llm.timeout_s),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_preferences_logic.py -v -k "llm_config"`

Expected: PASS for all four new tests. Then run the full file to make sure nothing else regressed: `pytest tests/test_preferences_logic.py -v`.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_llm.py tests/test_preferences_logic.py
git commit -m "feat(dictate_llm): resolve Gemini API key when provider=gemini or reuse opted in"
```

---

### Task 4: Thread `reuse_gemini_key` through `LLMState`

**Files:**
- Modify: `agent_doctor/ui/preferences/llm_tab.py` (whole file)
- Test: `tests/test_preferences_logic.py` (new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preferences_logic.py`:

```python
def test_llm_state_threads_reuse_gemini_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    state = lt.LLMState(
        provider_id="custom",
        base_url="http://localhost:8080/v1",
        model="qwen3",
        api_key=None,
        timeout_s=30,
        optimize_prompt=None,
        reuse_gemini_key=True,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.llm.reuse_gemini_key is True

    state2 = lt.LLMState.from_settings()
    assert state2.reuse_gemini_key is True


def test_llm_state_gemini_provider_accepts_default_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    state = lt.LLMState(
        provider_id="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-2.5-flash",
        api_key=None,
        timeout_s=30,
        optimize_prompt=None,
        reuse_gemini_key=False,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.llm.provider_id == "gemini"


def test_llm_state_gemini_provider_rejects_custom_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    with pytest.raises(lt.LLMStateError, match="custom"):
        lt.LLMState(
            provider_id="gemini",
            base_url="https://example.com/v1",
            model=None,
            api_key=None,
            timeout_s=30,
            optimize_prompt=None,
            reuse_gemini_key=False,
        ).apply()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_preferences_logic.py -v -k "reuse_gemini_key or gemini_provider"`

Expected: FAIL — `LLMState.__init__() got an unexpected keyword argument 'reuse_gemini_key'`.

- [ ] **Step 3: Add the field to `LLMState`**

Replace the contents of `agent_doctor/ui/preferences/llm_tab.py` with:

```python
"""LLM tab logic (provider, base_url, model, optimize prompt, gemini reuse)."""

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
    reuse_gemini_key: bool = False

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
            reuse_gemini_key=s.llm.reuse_gemini_key,
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
            reuse_gemini_key=bool(self.reuse_gemini_key),
        )
        ds.save(ds.replace_section(s, llm=new))


def probe_providers(timeout: float = 5.0) -> List[dl.ProbeResult]:
    return dl.probe_all(timeout=timeout)


def probe_one(provider_id: str, *, timeout: float = 5.0) -> dl.ProbeResult:
    """Probe a single provider, resolving the API key the same way ``llm_config`` does.

    Used by the "Test connection" button in the LLM tab so the gemini provider
    (and the reuse-checkbox path) gets the stored Gemini key as a Bearer token
    instead of always probing anonymously.
    """

    from agent_doctor import settings as gs

    s = ds.load()
    provider = dl.get_provider(provider_id)
    api_key: Optional[str] = None
    if provider_id == "gemini" or s.llm.reuse_gemini_key:
        api_key = gs.load_gemini_key()
    result = dl.probe(
        provider.base_url,
        provider.models_endpoint,
        timeout=timeout,
        api_key=api_key,
    )
    return dl.ProbeResult(
        provider_id=provider_id,
        base_url=result.base_url,
        reachable=result.reachable,
        models=result.models,
        error=result.error,
    )


def fetch_models_for(provider_id: str, base_url: Optional[str] = None, *, timeout: float = 5.0):
    p = dl.get_provider(provider_id)
    url = base_url or p.base_url
    return dl.probe(url, p.models_endpoint, timeout=timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_preferences_logic.py -v -k "reuse_gemini_key or gemini_provider"`

Expected: PASS for the three new tests.

Run the full file to confirm nothing regressed: `pytest tests/test_preferences_logic.py -v`.

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/llm_tab.py tests/test_preferences_logic.py
git commit -m "feat(prefs): thread reuse_gemini_key through LLMState; add probe_one helper"
```

---

### Task 5: Add a `probe_one` test that exercises the gemini key path

**Files:**
- Test: `tests/test_preferences_logic.py` (new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preferences_logic.py`:

```python
def test_llm_state_probe_one_passes_gemini_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe_one must pass the stored Gemini key as the Bearer token when
    the selected provider is ``gemini``, even if probe() can't actually reach
    the network (the assertion is about *what* is passed, not whether the
    upstream call succeeds)."""

    from agent_doctor import dictate_llm as dl
    from agent_doctor import settings as gs

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(gs, "load_gemini_key", lambda: "gemini-key-X")

    ds.save(ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(provider_id="gemini"),
    ))

    captured: dict[str, object] = {}

    def fake_probe(base_url: str, models_endpoint: str, *, timeout: float, api_key=None):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return dl.ProbeResult(
            provider_id="",
            base_url=base_url,
            reachable=True,
            models=["gemini-2.5-flash"],
            error=None,
        )

    monkeypatch.setattr(dl, "probe", fake_probe)
    result = lt.probe_one("gemini", timeout=0.1)
    assert result.reachable is True
    assert captured["api_key"] == "gemini-key-X"
    assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_preferences_logic.py::test_llm_state_probe_one_passes_gemini_key -v`

Expected: PASS (Task 4 already implemented `probe_one`; this test is just locking in the behaviour).

If it fails because `probe_one` is missing the `api_key` plumbing, fix `probe_one` in `llm_tab.py` per Task 4 step 3 before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_preferences_logic.py
git commit -m "test(prefs): assert probe_one forwards the Gemini key as a Bearer token"
```

---

### Task 6: Bump window geometry and rewrite the LLM tab UI

**Files:**
- Modify: `agent_doctor/ui/preferences/__init__.py:38-39` (window geometry) and `:106-188` (`_build_llm_tab`)

This task is UI code only — no automated test exists for the tk widget tree (matches the existing project pattern: logic is tested, layout is verified manually).

- [ ] **Step 1: Bump the window geometry**

In `agent_doctor/ui/preferences/__init__.py`, in `open_window()`, replace:

```python
    win = tk.Tk()
    win.title("agent-doctor — Preferences")
    win.geometry("520x440")
```

with:

```python
    win = tk.Tk()
    win.title("agent-doctor — Preferences")
    win.geometry("640x560")
    win.resizable(True, True)
```

- [ ] **Step 2: Rewrite `_build_llm_tab`**

In `agent_doctor/ui/preferences/__init__.py`, replace the entire `_build_llm_tab` function (currently lines 106-188) with:

```python
def _build_llm_tab(notebook: Any, lt: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

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
    ttk.Entry(txn_frame, textvariable=model_var).grid(row=2, column=1, sticky="ew", pady=4)

    reuse_var = tk.BooleanVar(value=state.reuse_gemini_key)
    reuse_chk = ttk.Checkbutton(
        txn_frame,
        text="Reuse Gemini API key from image generation",
        variable=reuse_var,
    )
    reuse_chk.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))

    txn_btn_row = ttk.Frame(txn_frame)
    txn_btn_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

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

    def on_provider_change(*_args: Any) -> None:
        # When the user picks 'gemini', snap the base URL to the canonical
        # endpoint so apply() doesn't reject the previous URL.
        from agent_doctor import dictate_llm as _dl
        try:
            p = _dl.get_provider(prov_var.get())
        except _dl.DictateLLMError:
            return
        if not p.allow_base_url_edit:
            url_var.set(p.base_url)
        refresh_visibility()
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
```

Note: this function imports `from agent_doctor import dictate_settings as ds` indirectly — but since the existing file in the project doesn't have that import at module scope (it's done via the `_build_*` helpers), add a local import at the top of `_build_llm_tab` if needed. Specifically, the `edit_prompt` inner function calls `ds.load()` / `ds.LLMSettings` / `ds.replace_section` / `ds.save`. Add this line near the top of `_build_llm_tab`:

```python
    from agent_doctor import dictate_settings as ds
```

(Place it right after `from agent_doctor import settings as gs`.)

- [ ] **Step 3: Smoke-test the UI manually**

Run: `python -m agent_doctor.cli prefs` (or whatever opens the preferences window — check `agent_doctor/cli.py` for the exact subcommand; the install pattern is `agent-doctor prefs` or `agent-doctor preferences`).

If unsure, just open a Python REPL:

```bash
python -c "from agent_doctor.ui.preferences import open_window; open_window()"
```

Visually verify:
- Window is 640x560 and resizable.
- LLM tab shows two LabelFrames.
- "Image generation" section: provider/model labels visible, API key status reads correctly, `Set…` opens the modal, `Clear` confirms.
- "Transcribed message handling": provider dropdown has four entries; switching to `gemini` snaps the URL to `https://generativelanguage.googleapis.com/v1beta/openai` and hides the reuse checkbox.
- Selecting non-gemini provider with no key: reuse checkbox is greyed out.
- Selecting non-gemini provider after setting a key: reuse checkbox is enabled.
- Buttons (`Set…`, `Clear`, `Test connection`, `Edit optimize prompt…`) all sit fully within the window.

If any of those fail, fix inline before committing.

- [ ] **Step 4: Re-run the test suite to confirm no logic regressed**

Run: `pytest tests/test_preferences_logic.py -v`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/__init__.py
git commit -m "feat(prefs): split LLM tab into image-gen + transcription sections; fix cutoff"
```

---

### Task 7: Full suite + cleanup

**Files:** none

- [ ] **Step 1: Run the full project test suite**

Run: `pytest -x`

Expected: all pass. If any unrelated test fails, investigate (don't suppress).

- [ ] **Step 2: Lint / type-check if the project has them configured**

Check `pyproject.toml` for `[tool.ruff]` / `[tool.mypy]` sections. If present, run the corresponding command (e.g., `ruff check .` or `mypy agent_doctor`). If absent, skip.

- [ ] **Step 3: Visual sanity check (one more time)**

Open the preferences window:

```bash
python -c "from agent_doctor.ui.preferences import open_window; open_window()"
```

- Toggle through all four providers; observe the reuse-checkbox visibility rules.
- Set a key, then clear it, then set it again. Watch the status label change live.
- Run `Test connection` against `lm_studio` (default) — expect either "OK — N model(s)" or "unreachable" depending on whether LM Studio is running. Either is fine; the test is that no crash occurs.

- [ ] **Step 4: Final commit (if anything changed in step 1-3)**

If nothing changed, skip. Otherwise:

```bash
git add -u
git commit -m "chore(prefs): final polish from manual smoke"
```

---

## Self-Review Notes

- **Spec coverage:** Window/layout fix → Task 6 step 1. Two LabelFrames → Task 6 step 2. New `reuse_gemini_key` field → Task 1. New `gemini` provider → Task 2. Extended `llm_config()` precedence → Task 3. Threaded through `LLMState` → Task 4. `Test connection` resolves the key → Task 4 (`probe_one`) + Task 5 (test). Set/Clear modal → Task 6 step 2. Checkbox visibility rules → Task 6 step 2 (`refresh_visibility`).
- **Test plan from spec:** all 8 test cases covered (Task 1 ×3, Task 2 ×2, Task 3 ×4, Task 4 ×3, Task 5 ×1 — note: 3 in Task 1, 2 in Task 2, 4 in Task 3, 3 in Task 4, 1 in Task 5 = 13 new tests, exceeding the spec's minimum of 8).
- **Backwards compatibility:** legacy JSON without `reuse_gemini_key` → covered by Task 1 step 1's third test.
- **No circular imports:** `dictate_llm.llm_config()` does `from . import settings as _gs` locally (Task 3 step 3), and `ui.preferences.llm_tab.probe_one()` does the same.
- **No placeholders:** every code block is concrete; every command shows expected outcome.
