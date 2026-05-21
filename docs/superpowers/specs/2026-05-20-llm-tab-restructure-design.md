# LLM Tab Restructure — Image generation + Transcription LLM

**Status:** Design approved 2026-05-20.
**Scope:** Preferences window → LLM tab. Fix display cutoff and split the tab into two LabelFrames: one for Gemini-only image generation (pet sprite), one for the existing transcribed-message LLM with optional Gemini key reuse.

## Problem

The LLM tab in the Preferences window currently has two issues:

1. **Visual cutoff.** Default window geometry is `520x440`. Entry fields (Base URL, Model) and right-anchored buttons (`Test connection`, `Edit optimize prompt…`) overflow the visible area on stock macOS Tk. Screenshot evidence: the `Test connection` and `Edit optimize prompt…` buttons are clipped against the right edge.
2. **Two unrelated LLM concerns share one flat list.** The Gemini API key powers `pet-generate-sprite` (image generation via `gemini-3-pro-image-preview`) but is invisible in the UI — only set/cleared via CLI. The transcribed-message LLM uses lm_studio / ollama / custom (no Gemini text option). Users who already have a Gemini key cannot reuse it for transcription handling without manually copying it into a Custom provider's Authorization header.

## Goals

- Fix the cutoff at all default macOS Tk DPIs.
- Make the Gemini API key (used for sprite generation) discoverable and managable from the Preferences window — set/clear without dropping to the CLI.
- Let the user opt into reusing the same Gemini key for transcribed-message LLM calls, either by picking `gemini` as the provider or by enabling a checkbox while staying on another provider.
- Backwards compatible: existing `~/.agent-doctor/dictate.json` continues to load; existing CLI commands keep working unchanged.

## Non-goals

- No new image-generation use case beyond the existing sprite pipeline. The "Image generation" section surfaces what's already wired up; it does not add new image features.
- No async live validation of the Gemini key against the Gemini API.
- No GUI key-rotation viewer, audit-log viewer, or backend (Keychain vs file) selector. The CLI `settings show` already covers diagnostics.
- No UI snapshot tests (tkinter is awkward to test headlessly). Logic-layer tests only, matching the existing pattern in `tests/test_preferences_logic.py`.

## Design

### Window + layout

- `agent_doctor/ui/preferences/__init__.py:39` — change `win.geometry("520x440")` to `win.geometry("640x560")`. Add `win.resizable(True, True)` (currently implicit; explicit for clarity).
- In `_build_llm_tab`, configure `frame.columnconfigure(1, weight=1)` so Entry widgets stretch with the window. Anchor the right-hand buttons (`Test connection`, `Edit optimize prompt…`) to `sticky="w"` rather than `"e"` so they never disappear off the right edge when widths vary.

Window stays a single fixed-size default; section heights fit within the new 560-pixel height. No scrollbar.

### LLM tab: two LabelFrames

```
┌─ Image generation (Gemini only) ─────────────────────┐
│  Provider:  Gemini (Nano Banana 2)         (label)   │
│  Model:     gemini-3-pro-image-preview     (label)   │
│  API key:   configured (Keychain)                    │
│             [ Set… ]  [ Clear ]                      │
└──────────────────────────────────────────────────────┘

┌─ Transcribed message handling ───────────────────────┐
│  Provider: [ lm_studio ▾ ]  (lm_studio/ollama/       │
│                              custom/gemini)          │
│  Base URL: [ http://localhost:1234/v1            ]   │
│  Model:    [ qwen3.6-35b…                        ]   │
│  [✓] Reuse Gemini API key from image generation      │
│       (greyed out when no key is configured)         │
│  [Test connection]  [Edit optimize prompt…]          │
└──────────────────────────────────────────────────────┘
```

#### Image generation section

- Read-only labels for provider name ("Gemini (Nano Banana 2)") and model id (`gemini-3-pro-image-preview`, from `agent_doctor.gemini_image.NANO_BANANA_2_MODEL`). Not user-editable — Gemini is the only image provider.
- API key status line reads from `settings.settings_status()`. Three rendered states:
  - `configured (Keychain)` — `Backend.KEYRING` with `configured=True`.
  - `configured (file)` — `Backend.FILE` with `configured=True`.
  - `not configured` — anything else.
- `Set…` button opens a small Toplevel modal containing a masked `ttk.Entry(show="*")` with a Save button. On save, calls `settings.store_gemini_key(value)`. Errors (empty key, keyring failure) are surfaced via `messagebox.showerror`. After save, the status label rerenders.
- `Clear` button calls `settings.clear_gemini_key()` after a `messagebox.askyesno` confirm. Status rerenders.
- The `Reuse Gemini API key` checkbox in the second section is enabled/disabled based on this section's status; toggling the key state must update the checkbox's enabled flag in real time.

#### Transcribed-message section

- Adds `gemini` as a 4th provider in `agent_doctor/dictate_llm.py`:

  ```python
  Provider(
      id="gemini",
      label="Gemini (OpenAI-compatible)",
      base_url="https://generativelanguage.googleapis.com/v1beta/openai",
      models_endpoint="/models",
      requires_api_key=True,
      allow_base_url_edit=False,
  ),
  ```

  Note: Gemini's OpenAI-compatible endpoint accepts the `Authorization: Bearer <key>` header that `dictate.py` already emits.

- The provider Combobox now has four options: `lm_studio`, `ollama`, `custom`, `gemini`.
- `Reuse Gemini API key` checkbox visibility rules:
  - Hidden when provider is `gemini` (redundant — the gemini provider always uses the stored key).
  - Visible and enabled when provider is `lm_studio` / `ollama` / `custom` AND a Gemini key is configured.
  - Visible but disabled (greyed) when provider is non-gemini AND no Gemini key is configured.
- Truth table for key resolution in `dictate_llm.llm_config()`:

  | Provider  | reuse_gemini_key | Key source                                  |
  |-----------|------------------|---------------------------------------------|
  | gemini    | any              | `settings.load_gemini_key()` (forced)       |
  | non-gemini| true             | `settings.load_gemini_key()` (opt-in)       |
  | non-gemini| false            | existing chain (kwarg / env / None)         |

  Explicit kwargs and `LLM_API_KEY` env still win when set; the Gemini reuse only fires for the otherwise-None branch. This preserves the current CLI override behaviour.

- `Test connection` resolves the API key the same way `llm_config()` does (kwarg / env / gemini-reuse / None) and passes it to `dictate_llm.probe()` via the existing `api_key` parameter. Implementation: extend `llm_tab.probe_providers()` (or add a sibling helper `probe_selected_provider`) to fetch the resolved key from `settings.load_gemini_key()` when provider is `gemini` or `reuse_gemini_key` is True, then call `probe(base_url, models_endpoint, api_key=resolved_key)` for that one provider. For non-gemini providers without reuse, behaviour is unchanged. When no key is configured and the gemini endpoint is selected, the probe returns `reachable=False, error="HTTP 401 …"` (Gemini's own response) — no special-cased "no key configured" branch; the existing error surface is enough.
- `Edit optimize prompt…` is unchanged.

### Data model + persistence

- `agent_doctor/dictate_settings.py` — `LLMSettings` gains one new field:

  ```python
  @dataclass(frozen=True)
  class LLMSettings:
      provider_id: str = "lm_studio"
      base_url: str = "http://localhost:1234/v1"
      model: Optional[str] = None
      api_key_ref: Optional[str] = None
      timeout_s: int = 30
      optimize_prompt: Optional[str] = None
      reuse_gemini_key: bool = False  # new
  ```

  `_to_dict` and `_from_dict` get one new entry each. No schema-version bump — defaults are read-compatible with pre-existing JSON (`llm_d.get("reuse_gemini_key", False)`).

- The Gemini API key itself stays in `agent_doctor.settings` (Keychain-first, TOML fallback). Not duplicated into `dictate.json`. The new bool is the only added persistence surface.

- `dictate_llm.llm_config()` precedence order, extended:
  1. explicit `api_key` kwarg
  2. `LLM_API_KEY` env var (`ENV_LLM_KEY`)
  3. **(new)** if `settings.llm.provider_id == "gemini"` OR `settings.llm.reuse_gemini_key` → `agent_doctor.settings.load_gemini_key()`
  4. None

  The Gemini fallback uses a local import (`from . import settings`) to avoid circular import risk.

### Files touched

- `agent_doctor/dictate_llm.py` — add `gemini` provider; extend `llm_config()` key resolution.
- `agent_doctor/dictate_settings.py` — add `reuse_gemini_key` field; update `_to_dict`/`_from_dict`.
- `agent_doctor/ui/preferences/llm_tab.py` — add `reuse_gemini_key` to `LLMState`; thread through `apply()` and `from_settings()`.
- `agent_doctor/ui/preferences/__init__.py` — rewrite `_build_llm_tab` for the two-LabelFrame layout; bump window geometry.
- `tests/test_preferences_logic.py` — new logic tests.

No changes to `agent_doctor/gemini_image.py`, `agent_doctor/cli.py`, or `agent_doctor/settings.py`.

### Test plan

Pure-logic tests, no Tk display required:

1. `test_llm_state_reuse_gemini_key_round_trip` — set `reuse_gemini_key=True`, save, load, assert preserved.
2. `test_llm_state_reuse_gemini_key_default_false_on_legacy_config` — write a `dictate.json` without the field, load, assert `False`.
3. `test_llm_state_gemini_provider_accepts_default_base_url` — `LLMState(provider_id="gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai", …).apply()` succeeds.
4. `test_llm_state_gemini_provider_rejects_custom_base_url` — same as above with a different URL; raises `LLMStateError` matching the existing `allow_base_url_edit=False` rule.
5. `test_llm_config_uses_gemini_key_when_provider_is_gemini` — monkeypatch `settings.load_gemini_key` to return `"key-A"`; assert `llm_config().api_key == "key-A"`.
6. `test_llm_config_uses_gemini_key_when_reuse_enabled` — provider `custom`, `reuse_gemini_key=True`, monkeypatched key; assert reused.
7. `test_llm_config_explicit_kwarg_overrides_gemini_reuse` — kwarg `api_key="kwarg-key"` + `reuse_gemini_key=True`; assert kwarg wins.
8. `test_providers_catalog_includes_gemini` — `dl.providers()` returns 4 ids including `gemini`.

## Risks and mitigations

- **Tkinter version differences on Tk 8.5 vs 8.6+** — LabelFrame and grid weights are supported on both. Tested by manually opening the Preferences window after the change (out-of-band; no automated UI test).
- **Gemini OpenAI-compatible endpoint shape may differ from Gemini-native** — the `/v1beta/openai/models` and `/v1beta/openai/chat/completions` paths are documented and stable; if Google changes them, the same code path that handles a broken `lm_studio` URL handles this. No silent failure: `Test connection` surfaces the error.
- **Key leakage in `Test connection` errors** — `dictate_llm.probe()` already returns error strings from urllib; the Gemini key is sent as a header, not as part of the URL, so it cannot end up in an HTTPError reason. Existing `redact_secret` is not needed here.
- **Existing user with no Gemini key, picks `gemini` provider** — `llm_config()` returns `api_key=None`, the HTTP call gets a 401 from Gemini, `Test connection` shows `HTTP 401 …`. No crash. The checkbox path is greyed out when no key, so the only way to hit this is to actively pick `gemini` while unconfigured — surfaced clearly.

## Open questions

None at design time.

## Out of scope (intentional)

- No new image-generation workflows.
- No GUI for the Keychain backend choice.
- No async key validation against Gemini.
- No telemetry on which provider users pick.
