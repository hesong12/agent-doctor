# Dictate Handy-parity design

**Status:** approved (brainstorming gate passed 2026-05-14)
**Owner:** Song He
**Project:** agent-doctor
**Slug:** `dictate-handy-parity`

## 1. Goal

Bring `agent-doctor dictate` to feature parity with the local Handy app — a Handy-style push-to-talk surface that records, transcribes locally, cleans the transcript with a local LLM, and delivers it to the user's clipboard / cursor. Reuse the existing pet display as the visual feedback surface, and surface every new knob in an organized Preferences window.

Six functional requirements (from `/goal`):

1. Whisper model picker + downloads from authorized endpoints (Handy-style catalog).
2. Configurable global hotkey for push-to-talk dictation.
3. Pet animation during recording ("listening" state).
4. LM Studio (and Ollama / Custom) integration with a single optimize-for-LLM prompt; pet animation during LLM call ("thinking" state); clipboard + optional auto-paste at cursor.
5. Organized frontend (Preferences window) covering every new setting.
6. Tests at every phase; quality gates green.

## 2. Non-goals

- Multi-platform parity. macOS-only for v1 (matches Handy and the existing dictate pipeline). Linux/Windows paths stay best-effort (dictate already documents this).
- Cloud LLM providers (OpenAI, Anthropic, Groq…). Local-LLM only via OpenAI-compatible HTTP. Custom URL covers everything else.
- Streaming transcription. Same record → transcribe → enhance pipeline as today.
- Rewriting the existing dictate state machine — additive only.
- Cross-session aggregation of dictate history (already there at the SQLite level).

## 3. Out-of-scope but adjacent

- Code signing / notarizing the Swift hotkey helper. Phase 4 builds locally with `swiftc`; signing is a packaging concern handled later.
- Auto-update / self-update for the Swift helper. Reinstall via CLI command.
- Localization of the Preferences window. English-only v1.

## 4. Architecture & shared infrastructure

### 4.1 Settings file

A new JSON config at `~/.agent-doctor/dictate.json`, separate from `~/.agent-doctor/config.toml` (which holds the Gemini secret). Schema-versioned, atomic-replace writes, mode `0600`, parent dir `0700` — matches the conventions in `agent_doctor/settings.py`.

```json
{
  "version": 1,
  "transcription": {
    "model_id": "ggml-large-v3-turbo",
    "model_path": "/Users/<user>/.agent-doctor/models/whisper/ggml-large-v3-turbo.bin",
    "language": "auto",
    "extra_buffer_ms": 150
  },
  "llm": {
    "provider_id": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "model": "qwen2.5-7b-instruct",
    "api_key_ref": null,
    "timeout_s": 30,
    "optimize_prompt": null
  },
  "hotkey": {
    "binding": "ctrl+option+space",
    "push_to_talk": true,
    "daemon_enabled": false
  },
  "paste": {
    "auto_paste": false,
    "paste_delay_ms": 60,
    "last_permission_check": null
  },
  "pet": {
    "animate_listening": true,
    "animate_thinking": true
  }
}
```

`provider_id` is one of `lm_studio | ollama | custom`. Provider catalog lives in code, not in the JSON, so adding a new provider doesn't require a settings migration.

`api_key_ref` is `null` (no key needed) or `"keychain:agent-doctor:dictate-llm-api-key"`. Actual key bytes live in the keychain via the existing `settings.py` keyring shim. The file never stores secrets.

### 4.2 New modules

- `agent_doctor/dictate_settings.py` — load / save / validate / migrate. Type-safe dataclasses. Exports `load() -> DictateSettings`, `save(settings)`, plus a `DEFAULTS` constant and a `migrate(payload)` function for future version bumps.
- `agent_doctor/dictate_models.py` — static authorized catalog + download helper.
- `agent_doctor/pet_transient.py` — context manager for transient pet states (listening / thinking) that does not clobber the autopilot-driven main snapshot.
- `agent_doctor/pet_animations.py` — pure-function animation draw helpers for tkinter canvas.
- `agent_doctor/hotkey/HotkeyHelper.swift` — Swift source for the global hotkey helper.
- `agent_doctor/hotkey_install.py` — build the Swift binary + write the launchd plist.
- `agent_doctor/ui/preferences/` — Preferences window + per-tab controllers.

### 4.3 Backward compatibility

- All existing dictate CLI flags (`--whisper-model`, `--backend`, `--llm-url`, `--llm-model`, `--llm-key`, `--mode`, `--no-enhance`, `--buffer-ms`, `--beep`, `--timing`, `--no-history`) continue to work. Precedence: CLI flag > env var > `dictate.json` > built-in default.
- `--mode chat|coding|research` are accepted but emit a one-line deprecation warning to stderr and collapse to the optimize prompt. `--mode raw` and `--no-enhance` keep their current meaning. The old `_MODE_PROMPTS` table is removed in Phase 2.
- `~/.agent-doctor/config.toml` (Gemini key) is untouched.

### 4.4 Cross-cutting test conventions

- No network in any unit test. HTTP is exercised against a `http.server.HTTPServer` started in a thread (already used elsewhere in this repo).
- No real audio devices, no real launchctl, no real osascript. Each side-effect runner is dependency-injected with a fake (this pattern already pervades `dictate.py`).
- Tkinter rendering tests are guarded with `pytest.mark.tkinter` and skip when `$DISPLAY` is unset; tab *controllers* (settings logic) are unit-tested headless.
- Each phase adds its tests; CI must be green before the next phase starts.

## 5. Phase 1 — Model picker + downloads

### 5.1 CLI

```
agent-doctor dictate models list                      # catalog + status
agent-doctor dictate models current                   # selected model
agent-doctor dictate models download <model_id> [--force]
agent-doctor dictate models set <model_id>            # update settings
agent-doctor dictate models remove <model_id>         # delete file
agent-doctor dictate models doctor                    # verify disk + hash
```

`models list` prints a fixed-width table: `ID  Status  Size  Notes`. JSON output via `--json`.

### 5.2 Catalog

Static list inside `dictate_models.py`, each entry:

```python
@dataclass(frozen=True)
class CatalogEntry:
    id: str                     # e.g. "ggml-large-v3-turbo"
    display_name: str           # "Large v3 Turbo (1.6 GB, recommended)"
    url: str                    # https://huggingface.co/ggerganov/whisper.cpp/resolve/main/<file>
    size_bytes: int
    sha256: str
    recommended_for: tuple[str, ...]   # ("cpu", "apple-silicon", "multilang")
```

Initial entries: `tiny`, `base`, `small`, `medium`, `large-v3`, `large-v3-turbo`, `large-v3-turbo-q5_0`. Sizes + SHA-256s sampled once from the upstream Hugging Face repo at design time.

### 5.3 Download flow

1. Resolve destination `~/.agent-doctor/models/whisper/<filename>`. Create parents `0700`.
2. Reject any URL outside the allow-list (`huggingface.co/ggerganov/whisper.cpp/resolve/main/`) — defense in depth in case the catalog gets tampered.
3. Stream with `urllib.request` to `<file>.part`, printing a single-line stderr progress bar (`HH:MM:SS | <id> | XX.X MB / YY.Y MB | ZZ.Z MB/s`). No new dep.
4. SHA-256 verify against catalog hash. Mismatch → delete `.part`, raise `DictateError` with both hashes.
5. `os.replace(<file>.part, <file>)` for atomic install.
6. If no model is currently selected in settings, auto-select.

`models doctor` re-verifies SHA-256 of installed models and offers to re-download mismatches.

### 5.4 Wiring into transcription

`dictate.transcribe()` already accepts a model name. After Phase 1, when `model_name is None`, it loads from `dictate_settings.load().transcription.model_path`. CLI flags still override (highest precedence).

### 5.5 Tests

`tests/test_dictate_models.py` (new):
- Catalog dataclass round-trip.
- Allow-list rejection: non-HF URLs raise.
- Download success against in-thread HTTP server serving canned bytes — assert SHA verified, atomic move, settings updated.
- SHA mismatch → `.part` deleted, error raised, no settings change.
- Partial download: simulate connection drop mid-stream; assert `.part` removed and clean error.
- `models set`, `models remove`, `models current` round-trip.

`tests/test_dictate.py` updated: when `model_name=None` and settings has `model_path`, `transcribe` resolves to that path.

## 6. Phase 2 — Optimize prompt + LM Studio / Ollama / Custom

### 6.1 Prompt simplification

Drop `_MODE_PROMPTS` (the chat / coding / research dict). Add a single `OPTIMIZE_PROMPT` constant: the existing `_BASE_RULES` body plus one closing line:

> Style: a clean, written prompt optimized for any downstream LLM. Use the user's language. No padding, no role-play preamble, no header — output the rewritten prompt only.

`mode_system_prompt()` is reduced to return `OPTIMIZE_PROMPT` for any input mode except `raw`. The function signature stays the same to limit blast radius. Settings can override the prompt via `llm.optimize_prompt` (string or null).

### 6.2 Provider catalog

```python
@dataclass(frozen=True)
class Provider:
    id: str                # "lm_studio" | "ollama" | "custom"
    label: str
    base_url: str
    models_endpoint: str   # "/models"
    requires_api_key: bool
    allow_base_url_edit: bool
```

Hard-coded in `agent_doctor/dictate_llm.py`. Probing a provider issues `GET <base_url><models_endpoint>` (10s timeout) and returns parsed `data: [{id, ...}]`. Unreachable → reachable=False, reason logged.

### 6.3 CLI

```
agent-doctor dictate llm probe        # try each, print reachability + models
agent-doctor dictate llm set --provider lm_studio --model <id> [--url ...]
agent-doctor dictate llm current
agent-doctor dictate llm test "<text>"
```

### 6.4 Wiring into enhancement

`llm_config_from_env()` becomes `llm_config()` and consults settings before env, env before CLI overrides. The HTTP call path is unchanged.

### 6.5 Tests

`tests/test_dictate_llm.py` (new):
- Provider probe against fake HTTP server (200 + JSON, 404, connection refused, timeout) — exhaustive matrix.
- Settings round-trip including custom URL.
- `llm test` enhances a canned transcript end-to-end with a stubbed provider.
- Optimize prompt rendering: stable string.
- Deprecation warning emitted when CLI passes `--mode chat|coding|research`.

Existing `tests/test_dictate*.py` updated to match the new single-mode prompt path.

## 7. Phase 3 — Pet listening + thinking animations

### 7.1 New states

`listening` and `thinking` are added to the pet state enum used by `pet_display.snapshot_from_payload()` and `_pet_action_detail`. Each gets an accent colour and a small accessible label:

| State      | Accent    | Label                |
| ---------- | --------- | -------------------- |
| listening  | `#33b3a8` | "Listening…"         |
| thinking   | `#e0a040` | "Optimizing prompt…" |

### 7.2 Transient state file

A new file `~/.agent-doctor/pet/pet-transient.json`:

```json
{ "state": "listening", "started_at": 1747234500.12, "expires_at": 1747234560.12, "owner": "dictate" }
```

`pet_display`'s read tick: read main `pet-status.json`, then read transient file. If transient exists, isn't expired, and `state` is one of {`listening`, `thinking`}, the rendered snapshot's `state` field is overlaid with the transient value while preserving every other field. Expired or missing → main snapshot wins. This guarantees autopilot-driven `intervening` is restored the moment dictation ends.

### 7.3 Context manager

```python
# agent_doctor/pet_transient.py
@contextmanager
def pet_state(state: str, *, ttl_seconds: float = 60.0) -> Iterator[None]:
    write_transient(state, ttl_seconds)
    try:
        yield
    finally:
        clear_transient(owner="dictate")
```

`dictate.run_pipeline` wraps the recording window with `pet_state("listening", ttl_seconds=180)` and the enhancement window with `pet_state("thinking", ttl_seconds=60)`.

### 7.4 Animations

`pet_animations.py` exposes two pure draw functions. Each is called from the existing `pet_display` tick loop with the canvas, the current `time.monotonic()` value, and the sprite centre:

- `draw_listening(canvas, t, cx, cy)` — soft cyan ring with alpha pulsing at 1.5 Hz (radius oscillates between R and R×1.18) plus a ±3px vertical sinusoidal bob at 1 Hz applied to the sprite image item.
- `draw_thinking(canvas, t, cx, cy)` — three amber dots orbiting at radius R+10px, 120° apart, 0.8 Hz orbit. No bob.

A canvas tag (`"dictate-animation"`) is deleted and redrawn each tick to avoid stacking items. When the transient state clears, the next tick deletes the tag and stops animating.

AppKit/Swift fallback path: no animation in v1, just a status-line update. Document in the spec; revisit if AppKit fallback gets used in practice.

### 7.5 Tests

- `tests/test_pet_transient.py` — context manager writes / clears, TTL expiry, owner check (only `dictate` owner's file is cleared).
- `tests/test_pet_overlay.py` — main snapshot + transient overlay precedence matrix.
- `tests/test_pet_animations.py` — draw functions exercise a fake canvas (records `create_oval`, `create_arc`, `coords` calls). Assert radius oscillates with `t`, dots traverse 360°, alpha within [0,1].
- `tests/test_dictate.py` — `run_pipeline` enters `listening` during record and `thinking` during enhance (verified by recording calls to a stubbed `pet_state`).

## 8. Phase 4 — Swift hotkey helper + launchd daemon

### 8.1 Helper

`agent_doctor/hotkey/HotkeyHelper.swift` (~150 LOC):

- On launch, read `~/.agent-doctor/dictate.json` once and parse `hotkey.binding`, `hotkey.push_to_talk`.
- Register `NSEvent.addGlobalMonitorForEvents(matching: [.keyDown, .keyUp])`.
- Match the binding (modifiers + key code). On match:
  - Toggle mode → spawn `agent-doctor dictate toggle`.
  - PTT mode → key-down: `agent-doctor dictate start`; key-up: `agent-doctor dictate stop`.
- On `SIGHUP`, re-read the config file (so the Preferences window can update binding without restarting the daemon).
- On `SIGTERM`, deregister monitor and exit cleanly.

### 8.2 Build + install

`agent_doctor/hotkey_install.py`:

1. `which swiftc` — abort with a clear message ("xcode-select --install required") if missing.
2. `swiftc HotkeyHelper.swift -O -o ~/Library/Application\ Support/agent-doctor/bin/agent-doctor-hotkey`.
3. `which agent-doctor` to discover the wrapper path; bake into the plist as `AGENT_DOCTOR_BIN`.
4. Write `~/Library/LaunchAgents/com.agent-doctor.hotkey.plist`:
   - `Label=com.agent-doctor.hotkey`.
   - `ProgramArguments=[<helper path>]`.
   - `EnvironmentVariables={"AGENT_DOCTOR_BIN": <abs path>}`.
   - `RunAtLoad=true`, `KeepAlive=true`.
5. `launchctl bootstrap gui/$(id -u) <plist>` (already used by `service.py`).
6. `launchctl print gui/$(id -u)/com.agent-doctor.hotkey` to verify.

### 8.3 CLI

```
agent-doctor dictate hotkey install
agent-doctor dictate hotkey set <chord>
agent-doctor dictate hotkey show
agent-doctor dictate hotkey test
agent-doctor dictate hotkey uninstall
```

`set` rewrites `hotkey.binding`, then `launchctl kill -SIGHUP gui/$(id -u)/com.agent-doctor.hotkey` to reload.

### 8.4 Chord parser

`parse_chord("ctrl+option+space")` → `Chord(modifiers={"ctrl", "option"}, key="space")`. Accept synonyms: `cmd|command`, `opt|option|alt`, `ctrl|control`, `shift`. Conflict list (refuse to set): `cmd+space`, `cmd+tab`, `cmd+q`, single-letter (no modifier).

### 8.5 Permissions

Input Monitoring permission required on macOS 13+. Install command prints the deep link `open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"` and waits for the user to confirm. After confirmation it re-tests by sending a synthetic event and checking the daemon's log line.

### 8.6 Tests

- `tests/test_hotkey_parse.py` — chord parser exhaustive matrix; conflict detection.
- `tests/test_hotkey_install.py` — install/uninstall against a temp `LaunchAgents` dir with `swiftc` and `launchctl` stubbed via PATH manipulation. Asserts plist content (XML diff) and launchctl invocation arguments.
- Manual: `make hotkey-smoke` builds + runs helper with `-h`. Not in CI.

## 9. Phase 5 — Auto-paste at cursor

### 9.1 Behavior

After `run_pipeline` writes to `pbcopy`:

```python
if settings.paste.auto_paste:
    time.sleep(settings.paste.paste_delay_ms / 1000)
    rc = osascript(['-e', 'tell application "System Events" to keystroke "v" using {command down}'])
    if rc != 0:
        notify("Auto-paste failed", "Text is on the clipboard — paste manually.")
```

Default OFF. Enabled only via the Preferences window after a permission test passes, or via `agent-doctor dictate paste enable` (which runs the same permission test).

### 9.2 Permission UX

`agent-doctor dictate paste enable`:
1. Write known-good text "agent-doctor paste test" to clipboard.
2. Run the osascript keystroke.
3. If non-zero → print "Accessibility permission required" and the deep-link `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`. Exit non-zero, settings unchanged.
4. If zero → set `paste.auto_paste = true` and record `last_permission_check` timestamp.

### 9.3 CLI

```
agent-doctor dictate paste enable
agent-doctor dictate paste disable
agent-doctor dictate paste test
```

### 9.4 Tests

`tests/test_dictate_paste.py`:
- enable / disable round-trip.
- Mocked osascript runner: rc=0 → settings updated; rc≠0 → notification posted, settings unchanged.
- `run_pipeline` skips paste when `auto_paste=false`.
- `run_pipeline` invokes paste with the configured delay when `auto_paste=true`.

## 10. Phase 6 — Preferences window

### 10.1 Surface

- Right-click pet → "Preferences…" menu item.
- `agent-doctor dictate preferences` CLI command for users without the pet open.
- Singleton tkinter `Toplevel()` — second invocation raises focus.

### 10.2 Tabs

(`ttk.Notebook` with 5 tabs; widget logic separated from settings logic.)

1. **Dictation**
   - Model dropdown (current + installed); "Download more…" → modal lister with size + per-row "Download" button + inline progress bar.
   - Language hint dropdown.
   - Extra recording buffer slider 0–500 ms.

2. **LLM**
   - Provider radio (LM Studio / Ollama / Custom).
   - Base URL entry (auto-fills per provider; editable when Custom).
   - Model dropdown (populated by `GET /v1/models`); Refresh button.
   - API key password entry (Custom only); stored in keychain.
   - "Test connection" button.
   - "Edit optimize prompt" → multiline text dialog.

3. **Hotkey**
   - Status (Running / Stopped / swiftc missing).
   - Binding capture button.
   - Push-to-talk vs Toggle radio.
   - Install / Uninstall buttons (Install disabled if no swiftc).

4. **Paste**
   - Auto-paste toggle (triggers permission test on enable).
   - Paste delay slider 0–250 ms.

5. **Pet**
   - Sprite preview + "Choose custom…" (existing `pet-set-sprite` flow).
   - "Animate during listening / thinking" toggles.

### 10.3 Layout

```
agent_doctor/ui/preferences/
  __init__.py            # PreferencesController, open_window()
  base.py                # tab-base helpers (settings binding, dirty tracking)
  dictation_tab.py
  llm_tab.py
  hotkey_tab.py
  paste_tab.py
  pet_tab.py
```

Each file <400 LOC. Settings are read on tab focus and written immediately on widget change (no global Save). Daemon-affecting changes (hotkey binding, daemon toggle) trigger `launchctl kickstart -k` + SIGHUP.

### 10.4 Tests

- `tests/test_preferences_logic.py` — per-tab *controller* logic, headless: input validation, settings round-trip, daemon-restart triggers (mocked), provider switching resets model dropdown.
- `tests/test_preferences_ui_smoke.py` — open + close each tab under a hidden tkinter root, marked `pytest.mark.tkinter`, auto-skip on display-less CI.

## 11. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Swift helper fails on macOS versions without swiftc | Phase 4 install gates on `which swiftc`; prints the `xcode-select --install` hint; settings reflect "helper not installed" state. |
| Input Monitoring / Accessibility permission gates degrade UX | Each phase that requires a permission has an explicit test command + deep-link to System Settings. We never silently fail. |
| Existing dictate users depend on chat/coding/research modes | Modes accepted as deprecated aliases for a release cycle (one minor version); deprecation warning to stderr; `CHANGELOG.md` calls it out. |
| Transient pet state leaked on crash | TTL on the transient file + `pet_display` ignores expired files. Worst case: ≤60s of stale state. |
| Model download from HF hangs | Hard 30s read-timeout and a single retry; progress bar shows MB/s so the user can ^C. |
| `pet_display.py` is already large (~42 KB) | Phase 3 extracts animations to `pet_animations.py` and overlay logic to `pet_transient.py`; Phase 6 keeps tabs in separate files. Net effect: split, not bloat. |

## 12. Phase ordering & gates

Each phase ships as its own PR. CI must be green before the next phase starts.

| # | Phase | Gate |
| --- | --- | --- |
| 1 | Models | `pytest -q` green; manual: download + transcribe small model. |
| 2 | Optimize + LLM | `pytest -q` green; manual: `dictate llm probe` + end-to-end with LM Studio. |
| 3 | Pet animations | `pytest -q` green; manual: see listening / thinking states during a dictate run. |
| 4 | Hotkey | `pytest -q` green; manual: install helper, press chord, recording starts. |
| 5 | Auto-paste | `pytest -q` green; manual: paste test succeeds, end-to-end dictate-to-cursor. |
| 6 | Preferences | `pytest -q` green; manual: all tabs read/write settings correctly. |

## 13. Open questions captured (none — all answered in brainstorming)

This document supersedes the open questions captured during the brainstorming session on 2026-05-14. All design decisions reflect user answers given in that session.
