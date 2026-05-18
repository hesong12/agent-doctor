# Hotkey tab — Handy-style UX redesign

**Status:** Approved design (2026-05-18). Implementation plan to follow via `superpowers:writing-plans`.
**Owner:** Song.
**Scope:** Replaces the current Preferences > Hotkey tab and the underlying chord model. Does not change the Whisper, LLM, Paste, or Pet tabs.

## 1. Why

The current Hotkey tab requires the user to bind a `modifier+key` chord (default `ctrl+option+space`). That works, but it's the wrong primitive for dictation: the most natural physical gesture is "hold a key, talk, release." Handy (handy.computer) popularised this gesture with single-modifier hold (Right Option by default), and users report the chord style feels like "I'm summoning a command palette" rather than "I'm talking."

The recording entry point itself stays a hotkey — there is **no UI button to start a recording**. What changes is how the user *configures* that hotkey: visual key-capture instead of a text field, single-modifier hold supported as a first-class primitive, daemon state surfaced inline, macOS permission gates surfaced inline.

## 2. Locked decisions

These were settled in brainstorm before this spec:

1. **Trigger model.** Single-modifier hold is first-class; multi-key chords remain supported as a fallback. Default binding changes from `ctrl+option+space` to **`right_cmd`** (hold Right Command).
2. **Layout.** Plumbing stays visible (background daemon toggle, Install/Uninstall, Show logs, mode segmented control). Permission banner is inline. See §4.
3. **Capture overlay.** Modeless: live-preview each key event, auto-commit a held-released single modifier after 400ms, show a "Use this chord" button when the captured binding is multi-key, Esc cancels. See §5.
4. **Status pill** has four states: `Listening` / `Permission needed` / `Paused` / `Daemon stopped`.
5. **Permission banner** deep-links to the first missing macOS pane (Accessibility, then Input Monitoring) and re-detects on window focus.
6. **Test** runs a stub event through the daemon → CLI roundtrip; it does not exercise Whisper. Pet flashes `listening` for 600ms; tab shows "Daemon received key event ✓" inline.
7. **Single-modifier + Toggle mode** is disabled (radio greyed out with tooltip "Toggle needs a key, not a modifier"). Chord bindings allow either mode.
8. **Show logs** opens `~/Library/Logs/agent-doctor-hotkey.log` via `open -a Console`.
9. **Uninstall** prompts a native confirm dialog ("Stop and remove the hotkey daemon?") with destructive styling.
10. **Migration.** Existing `ctrl+option+space` bindings remain valid and are preserved on load — no forced rewrite. New installs get `right_cmd`.

## 3. Non-goals

- Adding a "Start dictation" button anywhere in the UI. Recording entry stays hotkey-only.
- Supporting non-macOS platforms. macOS-only by design; the daemon already ships only on macOS via launchd.
- A standalone Hotkey window detached from Preferences. The redesign lives inside the existing Preferences notebook.
- Rewriting the Swift helper's event loop architecture. We extend the existing `HotkeyDaemon` class; we do not switch to Carbon Hotkey API or accessibility-driven simulation.
- Cloud-synced bindings, per-app bindings, or multi-binding profiles.

## 4. Hotkey tab anatomy (the redesigned panel)

The tab is a single vertically stacked panel inside the existing `ttk.Notebook`. From top to bottom:

```
┌── Hotkey tab ──────────────────────────────────────────┐
│  Global hotkey                  [ Listening • ]        │  ← Header row + status pill
│  Trigger dictation from anywhere on the system.        │
│                                                        │
│  ⚠ Accessibility permission required  [ Open settings… ]│  ← Permission banner (shown only if perms missing)
│                                                        │
│  SHORTCUT                                              │
│  ┌──────────────────────────────────────────────────┐ │
│  │ ⌘ Right Command   Hold to record    [ Record… ] │ │  ← Keycap tile + Record / Test button
│  │                   Click to re-record · Test     │ │
│  └──────────────────────────────────────────────────┘ │
│                                                        │
│  Mode             [ Push-to-talk | Toggle ]            │  ← Segmented control; Toggle greyed if modifier-only
│  Push-to-talk holds while pressed.                     │
│                                                        │
│  Background daemon            [ on/off switch ]        │  ← Toggle: stop the daemon without uninstalling
│  launchd LaunchAgent. Reloads on settings change.      │
│                                                        │
│                       [ Show daemon logs ] [ Uninstall ]│  ← Footer actions
└────────────────────────────────────────────────────────┘
```

### 4.1 Header row + status pill

- Title: "Global hotkey". Subtitle: "Trigger dictation from anywhere on the system."
- Status pill is right-aligned. States, each with a coloured dot + label:
  - **Listening** (green) — plist installed AND `launchctl print` reports running AND both macOS permissions granted.
  - **Permission needed** (amber) — daemon running but Accessibility or Input Monitoring missing.
  - **Paused** (grey) — plist installed but `Background daemon` toggle is off, or `launchctl print` reports not running for a transient reason.
  - **Daemon stopped** (grey) — plist not installed at all.
- The pill is recomputed every time the Preferences window gains focus, plus once per second while focused (lightweight `launchctl print` is fast enough).

### 4.2 Permission banner

Shown only when at least one of {Accessibility, Input Monitoring} is missing. Amber background.

- Single line of copy: "⚠ Accessibility permission required" or "⚠ Input Monitoring permission required" or "⚠ Accessibility and Input Monitoring permissions required."
- Right-side button: "Open settings…" — runs `open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"` (or the Input Monitoring URL) for whichever is missing first.
- Detection uses the same checks the CLI already has in `agent_doctor/cli.py:_cmd_dictate_hotkey_test` (look at the existing strings around line 2716).
- Banner refreshes on window focus.

### 4.3 Shortcut tile

A bordered card showing:

- **Left:** a stylised keycap with glyph (`⌘`, `⌥`, `⌃`, `⇧`, or `🌐` for Fn) and label (`Right Command`, `Fn`, `Left Option`, …). For chord bindings, render multiple keycaps side-by-side separated by `+`.
- **Middle:** "Hold to record" for modifier-only, "Press to record" for chord. Smaller hint below: "Click to re-record".
- **Right:** two compact buttons: `Record…` opens the capture overlay (§5); `Test` runs the stub roundtrip (§6.4).

The tile is itself clickable (whole-card click also opens Record…); the explicit `Record…` button is for affordance.

### 4.4 Mode segmented control

- Two segments: `Push-to-talk` (default) and `Toggle`.
- When the current binding is a single modifier, the `Toggle` segment is disabled and shows tooltip "Toggle needs a key, not a modifier". The `Push-to-talk` segment cannot be deselected in that case.
- When the current binding is a chord, both segments are enabled. Selecting one commits immediately and SIGHUPs the daemon.
- Subtitle text updates: PTT shows "Push-to-talk holds while pressed."; Toggle shows "Toggle flips on a press, flips again to stop."

### 4.5 Background daemon switch

- A toggle switch (Tk doesn't ship one natively; render as `ttk.Checkbutton` styled to look like a switch, or use a labelled `Radiobutton` pair — the implementation plan decides).
- ON = plist loaded, helper running. Driven by `hotkey_install.install()` / `hotkey_install.uninstall()` plus `_run_launchctl bootstrap/bootout`.
- OFF = plist file remains on disk, but the agent is bootout'd. This is the difference between "Paused" and "Daemon stopped": Paused means the user can flip it back instantly; Daemon stopped means we need to re-run the build+install cycle.

### 4.6 Footer actions

- `Show daemon logs` runs `open -a Console "~/Library/Logs/agent-doctor-hotkey.log"`. The log path is canonical (already used in `hotkey_install.write_plist`).
- `Uninstall` opens a native `messagebox.askyesno` confirm titled "Stop and remove the hotkey daemon?" with destructive copy. Yes → `hotkey_install.uninstall()` + helper binary unlink + UI flips to `Daemon stopped`. No → no-op.

## 5. Capture overlay (the Record… modal)

A `tk.Toplevel` centred on the Preferences window, ~420×320, no titlebar buttons, modal-grabs Preferences, and binds `<Key>`, `<KeyRelease>`, `<FocusOut>`.

### 5.1 States

The overlay is a state machine with 4 visible states (matches the brainstorm mockup):

1. **Idle (waiting).** Four dimmed keycaps (⌃ ⌥ ⌘ ⇧) shown. Helper text: "Hold a single modifier (⌘ ⌥ ⌃ ⇧ Fn) or press a chord." Countdown row reads "Waiting…". Esc cancels.
2. **Captured-modifier.** A single highlighted keycap appears as soon as a modifier press is detected. Countdown starts: "Hold for 0.4s · then release to commit". The 400ms is a minimum-hold filter — releasing before 400ms returns to **Idle** without committing (treats it as an accidental tap). Once the held duration crosses 400ms, the subtitle changes to "Release to commit"; releasing then commits the binding and closes the overlay. If the user adds a key while still holding the modifier, the overlay transitions to **Captured-chord**.
3. **Captured-chord.** All pressed keys render as separate keycap tiles. Subtitle changes to "Mode will switch to Toggle automatically". Auto-commit is disabled in this state — the user must click `Use this chord` (primary button). This prevents accidentally committing a chord the user pressed in passing.
4. **Conflict.** Shown when the captured chord is in `hotkey_parse.CONFLICT_CHORDS`. Red banner copy: "⌘ + Space is reserved by Spotlight. Pick another key." The primary button is disabled; the user must press another binding or Cancel.

### 5.2 Cancellation semantics

- `Esc` → close overlay, no change to settings.
- Window losing focus → same as Esc.
- The capture overlay does **not** kill the daemon while open. The daemon keeps running on the current binding until the new one is committed.

### 5.3 Modifier detection (Tk)

Tk's `<Key>` event carries `keysym` and `state` (bitmask of held modifiers). We extend `pet_display.py`'s existing chord recorder logic. For left/right disambiguation:

- Tk on macOS surfaces left/right modifier as different `keysym` values: `Meta_L` vs `Meta_R`, `Alt_L` vs `Alt_R`, `Control_L` vs `Control_R`, `Shift_L` vs `Shift_R`.
- `Fn` is **not** delivered as a normal Tk key event. For Fn detection in the capture overlay, we fall back to a one-shot Swift helper invocation: a tiny `swift run` snippet that returns the next `flagsChanged` event's modifier set. If that fails (no swiftc / no permission), the overlay shows the conflict-style message: "Fn capture isn't available — use a different modifier." Fn remains *daemon-bindable* if the user enters `fn` manually in the chord-fallback path (rare).

## 6. Data model changes

### 6.1 Chord schema (`hotkey_parse.py`)

Extend `Chord` to support modifier-only bindings:

```python
@dataclass(frozen=True)
class Chord:
    modifiers: Tuple[str, ...]   # canonical order: cmd, ctrl, option, shift, fn
    key: str | None              # None ⇔ modifier-only binding
    side: str | None = None      # "left" | "right" | None; only meaningful when key is None
```

- `canonical()`:
  - Modifier-only: `right_cmd`, `left_option`, `fn`, etc. — one token, no `+`.
  - Chord: `ctrl+option+space` — unchanged.
- New `KEY_TOKENS` entries: `left_cmd`, `right_cmd`, `left_option`, `right_option`, `left_ctrl`, `right_ctrl`, `left_shift`, `right_shift`, `fn`.
- `parse()` recognises these single-token forms as modifier-only and rejects "lone letter" bindings (`a`, `space`) the way it does today.
- `CONFLICT_CHORDS` is unchanged; no L/R conflict entries needed (system shortcuts use chords).
- New helper `is_modifier_only(chord: Chord) -> bool` for the Tk layer.

### 6.2 Settings schema (`dictate_settings.py`)

`HotkeySettings.binding` stays a single string. No schema-version bump needed — `right_cmd` is a new valid value of the same field.

`HotkeySettings.push_to_talk` is coerced to `True` at save time whenever `is_modifier_only(parse(binding))` is true. The UI also disables the Toggle segment in that case, but the coercion is enforced in `HotkeyState.apply()` regardless of caller (defensive — a stale settings file that contains `push_to_talk: false` with a modifier-only binding gets fixed on next save, and the Swift helper's modifier-only path always behaves as PTT anyway).

Default value changes:
```python
HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=False)
```

### 6.3 Migration

`dictate_settings.load()` does not rewrite the file. Users with an existing `ctrl+option+space` binding keep it; the new tile renders the chord form. Fresh installs (no `dictate.json` yet) get `right_cmd`.

## 7. Swift helper changes (`HotkeyHelper.swift`)

The helper currently listens for `[.keyDown, .keyUp]` events. We extend it to also listen for `.flagsChanged` so modifier-only bindings work.

### 7.1 New keycodes

Add to `KEYCODES`:

```swift
"left_cmd": 55, "right_cmd": 54,
"left_option": 58, "right_option": 61,
"left_ctrl": 59, "right_ctrl": 62,
"left_shift": 56, "right_shift": 60,
"fn": 63,
```

### 7.2 Parse extension

`parse(_:)` returns a `ParsedChord` with `modifiers` empty and `keyCode` set to the modifier-only virtual key code (e.g. 54 for Right Command). The daemon distinguishes the two cases by inspecting `chord.modifiers.isEmpty && KEYCODES_MODIFIER_ONLY.contains(chord.keyCode)`.

### 7.3 Event handling

- **Chord mode** (current code path): unchanged.
- **Modifier-only mode** (new): subscribe to `NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged)`. On each event, check `event.keyCode == bound_keycode` AND the modifier *flag* corresponding to that keycode is in the new `event.modifierFlags` (press) or absent (release). **Alone-key rule:** trigger only when *no other modifier flags* are held at the moment of press — this prevents Cmd+Right-Option style chords from firing the dictation start. If during an active recording another modifier becomes held, the daemon treats it as a release and runs `dictate stop`. On press (alone), run `dictate start`. On release, run `dictate stop`. There is no Toggle mode in this path — single modifier-only binding always behaves as PTT.

### 7.4 Race conditions

If the user releases very fast (< 80ms), the recording can be a no-op silence. The daemon does not need to debounce; the existing dictate pipeline already discards sub-threshold clips. The capture-modal's 400ms commit window is a UI affordance only; it does not affect the daemon.

## 8. UI plumbing (`agent_doctor/ui/preferences/`)

### 8.1 Files touched

- `hotkey_tab.py` — extend `HotkeyState` to carry the new fields needed by the redesign (current binding, parsed form, daemon status snapshot, permission status). Add `daemon_status()` wrapper, `permission_status()` wrapper.
- `__init__.py` — rewrite `_build_hotkey_tab` to render the new layout. The function gets larger; if it crosses ~250 LOC, extract a `hotkey_tab_view.py` for the widget composition (keep tab logic in `hotkey_tab.py`).
- New module `agent_doctor/ui/preferences/permission_probe.py` — single function `check_macos_permissions() -> PermissionStatus` returning `{accessibility: bool, input_monitoring: bool, first_missing: str | None}`. Headless-testable.
- New module `agent_doctor/ui/preferences/hotkey_capture.py` — the `Toplevel` overlay logic (idle / captured-modifier / captured-chord / conflict state machine). Headless-testable for the state machine; the Tk binding is a thin wrapper.

### 8.2 Headless testability

The pattern is the same one `dictation_tab.py` and `llm_tab.py` already follow: a `@dataclass` `…State` carries the values, an `apply()` method does the side effect, the tk shell is in `__init__.py`. Tests construct the state, call apply, and assert on `dictate_settings.load()`. The Capture overlay's state machine is tested by feeding key events into a pure-Python `CaptureController` and inspecting its output state, never opening a window.

## 9. Telemetry / safety

- No new telemetry.
- All settings writes go through the existing `dictate_settings.save()` atomic writer; capture overlay never writes partial state.
- Permission deep-links use `open` with the documented `x-apple.systempreferences:` URL scheme. No code injection surface.
- Daemon install path is unchanged; we already run `swiftc` against bundled source and `launchctl bootstrap` with a per-user domain target.

## 10. Acceptance criteria

The redesign is done when:

1. Opening Preferences > Hotkey on a fresh install shows the new layout with status pill `Daemon stopped` and a `Background daemon` switch in the off position.
2. Flipping `Background daemon` on builds + installs + bootstraps the LaunchAgent; the pill flips to `Permission needed` until the user grants both macOS perms, then `Listening`.
3. Clicking `Record…` opens the overlay. Holding Right Option for ≥400ms and releasing commits `right_option` as the binding, closes the overlay, and the keycap tile updates without re-opening Preferences.
4. Holding Ctrl+Option then pressing Space in the overlay shows the chord state with `Use this chord` button. Clicking it commits `ctrl+option+space`, Mode flips to Toggle automatically.
5. Pressing ⌘+Space in the overlay shows the conflict state; commit button is disabled.
6. `Test` button triggers a "Daemon received key event ✓" inline message within 500ms when the daemon is live; shows "Daemon not running" when it's not.
7. With a modifier-only binding, holding the bound key starts a recording (pet shows `listening`); releasing it stops and triggers the standard dictate finish pipeline.
8. `Show daemon logs` opens Console.app with the helper log preselected.
9. `Uninstall` shows the destructive confirm and on Yes flips the pill to `Daemon stopped` and removes plist + helper.
10. An existing user with `ctrl+option+space` in `dictate.json` opens the tab and sees their chord rendered as `⌃⌥Space` with Mode = Push-to-talk preserved.
11. Headless tests cover: `Chord.canonical()` for modifier-only, `parse()` for new tokens, `CaptureController` state transitions, `permission_probe` outcomes against a mocked `open` and `tccutil`, and `HotkeyState.apply()` for both binding shapes.

## 11. Open questions deferred to plan

These are deliberately *not* settled in this design; they're implementation choices the plan will pick:

- Tk switch widget styling (synthesise from `ttk.Checkbutton` vs use a third-party `ttkbootstrap` accent). Default: stock ttk to keep dependency surface unchanged.
- Whether to extract `hotkey_tab_view.py` immediately or keep widget composition inline in `__init__.py`. Default: inline first; extract if `__init__.py` crosses 250 LOC during implementation.
- Exact glyph mapping for the keycap tile (e.g. how to render Fn on Magic Keyboards that label it "🌐"). Default: use `🌐` if the binding is `fn`, else the standard ⌘ ⌥ ⌃ ⇧ glyphs.

## 12. References

- Current implementation: `agent_doctor/ui/preferences/__init__.py:191-267`, `agent_doctor/ui/preferences/hotkey_tab.py`, `agent_doctor/hotkey_parse.py`, `agent_doctor/hotkey_install.py`, `agent_doctor/hotkey/HotkeyHelper.swift`.
- Reference UX: Handy (handy.computer / cjpais/Handy) — single-modifier hold trigger, inline permission gating, status pill, modeless capture.
- Brainstorm mockups: `.superpowers/brainstorm/22561-1779131318/content/{01-default-key,02-layout,03-capture-modal}.html` (kept on disk for plan-time reference; gitignored).
