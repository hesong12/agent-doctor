# Dictate Phase 4 — Swift hotkey helper + launchd daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tiny user-installed Swift binary watches for a configurable global hotkey and shells out to `agent-doctor dictate toggle` (or `start`/`stop` in push-to-talk mode). The Python side ships the Swift source, builds it on demand with `swiftc`, writes a launchd plist, and exposes `agent-doctor dictate hotkey {install,set,show,test,uninstall}`.

**Architecture:** Source lives at `agent_doctor/hotkey/HotkeyHelper.swift`. A new Python module `hotkey_install.py` builds the helper, writes the launchd plist, and supports SIGHUP'ing the daemon on config changes. A small `hotkey_parse.py` validates / canonicalises chord strings and refuses dangerous conflicts. The CLI surface mirrors the existing dictate subcommand pattern.

**Tech Stack:** Python stdlib (subprocess, plistlib, pathlib, argparse). Swift (NSEvent global monitor) built at install time with `swiftc`. No new Python runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-05-14-dictate-handy-parity-design.md` §8.

**Prereq:** Phases 1–3 landed. `dictate_settings.HotkeySettings` exists.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_doctor/hotkey_parse.py` | Create — parse/canonicalise/validate chord strings; conflict detection. |
| `agent_doctor/hotkey_install.py` | Create — build Swift helper, write launchd plist, bootstrap/teardown, SIGHUP. |
| `agent_doctor/hotkey/HotkeyHelper.swift` | Create — global NSEvent monitor that shells out to agent-doctor. |
| `agent_doctor/cli.py` | Modify — register `dictate hotkey {install,set,show,test,uninstall}`. |
| `pyproject.toml` | Modify — include the Swift source in `package-data`. |
| `tests/test_hotkey_parse.py` | Create — parser + conflict matrix. |
| `tests/test_hotkey_install.py` | Create — build/install/uninstall against tmp dirs with stubs. |
| `tests/test_cli_subcommand_registration.py` | Modify — register the new subcommands. |

---

## Task 1: Chord parser + conflict detection (`hotkey_parse.py`)

**Files:**
- Create: `agent_doctor/hotkey_parse.py`
- Test: `tests/test_hotkey_parse.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hotkey_parse.py`:

```python
"""Tests for chord parsing + conflict detection."""

from __future__ import annotations

import pytest

from agent_doctor import hotkey_parse as hp


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+option+space", "ctrl+option+space"),
        ("CTRL + OPT + SPACE", "ctrl+option+space"),
        ("control + alt + space", "ctrl+option+space"),
        ("cmd+shift+d", "cmd+shift+d"),
        ("option+space", "option+space"),
    ],
)
def test_parse_canonical(raw: str, expected: str) -> None:
    chord = hp.parse(raw)
    assert chord.canonical() == expected


def test_parse_empty_raises() -> None:
    with pytest.raises(hp.HotkeyParseError, match="empty"):
        hp.parse("")


def test_parse_requires_a_modifier() -> None:
    with pytest.raises(hp.HotkeyParseError, match="modifier"):
        hp.parse("space")


def test_parse_rejects_unknown_token() -> None:
    with pytest.raises(hp.HotkeyParseError, match="unknown"):
        hp.parse("ctrl+banana")


@pytest.mark.parametrize(
    "raw",
    ["cmd+space", "cmd+tab", "cmd+q", "cmd+w"],
)
def test_known_conflicts_are_rejected(raw: str) -> None:
    with pytest.raises(hp.HotkeyParseError, match="conflict"):
        hp.parse(raw)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_hotkey_parse.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the parser**

Create `agent_doctor/hotkey_parse.py`:

```python
"""Chord string parser for the global hotkey helper.

Canonical form: ``mod1+mod2+key`` with modifiers in this order: cmd, ctrl,
option, shift. Lowercase. Key tokens are short identifiers (``space``,
``return``, ``f1``, single letters, digits).

Conflicts are explicitly refused for chords that collide with macOS system
shortcuts (cmd+space spotlight, cmd+tab app-switcher, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

MODIFIER_ALIASES = {
    "cmd": "cmd",
    "command": "cmd",
    "ctrl": "ctrl",
    "control": "ctrl",
    "option": "option",
    "opt": "option",
    "alt": "option",
    "shift": "shift",
}
MODIFIER_ORDER = ("cmd", "ctrl", "option", "shift")

KEY_TOKENS = frozenset(
    {"space", "return", "enter", "escape", "tab", "delete", "backspace"}
    | {chr(c) for c in range(ord("a"), ord("z") + 1)}
    | {chr(c) for c in range(ord("0"), ord("9") + 1)}
    | {f"f{n}" for n in range(1, 13)}
)

CONFLICT_CHORDS: FrozenSet[str] = frozenset(
    {
        "cmd+space",
        "cmd+tab",
        "cmd+q",
        "cmd+w",
        "cmd+option+escape",
        "cmd+shift+3",
        "cmd+shift+4",
        "cmd+shift+5",
    }
)


class HotkeyParseError(ValueError):
    pass


@dataclass(frozen=True)
class Chord:
    modifiers: Tuple[str, ...]
    key: str

    def canonical(self) -> str:
        return "+".join((*self.modifiers, self.key))


def parse(raw: str) -> Chord:
    if not raw or not raw.strip():
        raise HotkeyParseError("empty hotkey")
    tokens = [t.strip().lower() for t in raw.replace(",", "+").split("+") if t.strip()]
    if not tokens:
        raise HotkeyParseError("empty hotkey")

    modifiers: set[str] = set()
    keys: list[str] = []
    for tok in tokens:
        if tok in MODIFIER_ALIASES:
            modifiers.add(MODIFIER_ALIASES[tok])
            continue
        if tok in KEY_TOKENS:
            keys.append(tok)
            continue
        raise HotkeyParseError(f"unknown token in hotkey: {tok!r}")

    if not modifiers:
        raise HotkeyParseError(
            f"hotkey requires at least one modifier (got {raw!r})"
        )
    if len(keys) != 1:
        raise HotkeyParseError(
            f"hotkey must have exactly one key (got {len(keys)} in {raw!r})"
        )

    ordered = tuple(m for m in MODIFIER_ORDER if m in modifiers)
    canonical_str = "+".join((*ordered, keys[0]))
    if canonical_str in CONFLICT_CHORDS:
        raise HotkeyParseError(
            f"hotkey {canonical_str} conflicts with a macOS system shortcut"
        )
    return Chord(modifiers=ordered, key=keys[0])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hotkey_parse.py -v`
Expected: 12 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/hotkey_parse.py tests/test_hotkey_parse.py
git commit -m "feat(hotkey): chord parser with macOS conflict detection"
```

---

## Task 2: Add the Swift source

**Files:**
- Create: `agent_doctor/hotkey/HotkeyHelper.swift`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write the Swift source**

Create `agent_doctor/hotkey/HotkeyHelper.swift`:

```swift
// Global hotkey helper for agent-doctor dictate.
//
// Reads ``~/.agent-doctor/dictate.json`` to discover the binding + push-to-talk
// flag. Registers an NSEvent global monitor. On match, shells out to
// ``agent-doctor dictate toggle`` (toggle mode) or ``dictate start`` / ``stop``
// (PTT mode). Re-reads config on SIGHUP. Exits cleanly on SIGTERM.

import Cocoa
import Foundation

struct Config {
    var binding: String
    var pushToTalk: Bool
    var agentDoctorBin: String
}

func defaultConfigPath() -> URL {
    let home = FileManager.default.homeDirectoryForCurrentUser
    return home.appendingPathComponent(".agent-doctor/dictate.json")
}

func defaultBin() -> String {
    if let env = ProcessInfo.processInfo.environment["AGENT_DOCTOR_BIN"] {
        return env
    }
    return "/usr/local/bin/agent-doctor"
}

func readConfig() -> Config {
    let path = defaultConfigPath()
    let fallback = Config(
        binding: "ctrl+option+space",
        pushToTalk: true,
        agentDoctorBin: defaultBin()
    )
    guard let data = try? Data(contentsOf: path) else { return fallback }
    guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    else { return fallback }
    let hotkey = (json["hotkey"] as? [String: Any]) ?? [:]
    return Config(
        binding: (hotkey["binding"] as? String) ?? fallback.binding,
        pushToTalk: (hotkey["push_to_talk"] as? Bool) ?? fallback.pushToTalk,
        agentDoctorBin: defaultBin()
    )
}

let MODIFIERS: [String: NSEvent.ModifierFlags] = [
    "cmd": .command,
    "ctrl": .control,
    "option": .option,
    "shift": .shift,
]

// Common keycodes on US ANSI layout. Extend as needed.
let KEYCODES: [String: UInt16] = [
    "space": 49, "return": 36, "enter": 76, "escape": 53, "tab": 48,
    "delete": 51, "backspace": 51,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35,
    "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
]

struct ParsedChord {
    var modifiers: NSEvent.ModifierFlags
    var keyCode: UInt16
}

func parse(_ binding: String) -> ParsedChord? {
    let tokens = binding.lowercased().split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
    var mods: NSEvent.ModifierFlags = []
    var keyCode: UInt16? = nil
    for tok in tokens {
        if let flag = MODIFIERS[tok] {
            mods.insert(flag)
        } else if let kc = KEYCODES[tok] {
            keyCode = kc
        }
    }
    guard let code = keyCode else { return nil }
    return ParsedChord(modifiers: mods, keyCode: code)
}

func run(_ argv: [String]) {
    let proc = Process()
    proc.launchPath = argv[0]
    proc.arguments = Array(argv.dropFirst())
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = FileHandle.nullDevice
    do { try proc.run() } catch {
        fputs("hotkey: failed to launch \(argv): \(error)\n", stderr)
    }
}

class HotkeyDaemon {
    var config: Config = readConfig()
    var chord: ParsedChord? = nil
    var monitor: Any? = nil
    var keyDown = false

    func reload() {
        config = readConfig()
        chord = parse(config.binding)
        if let m = monitor {
            NSEvent.removeMonitor(m)
            monitor = nil
        }
        guard let c = chord else {
            fputs("hotkey: could not parse binding \(config.binding)\n", stderr)
            return
        }
        let mask: NSEvent.EventTypeMask = [.keyDown, .keyUp]
        monitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] ev in
            guard let self = self else { return }
            let flags = ev.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if ev.keyCode != c.keyCode { return }
            if !flags.contains(c.modifiers) { return }
            if ev.type == .keyDown {
                if !self.keyDown {
                    self.keyDown = true
                    if self.config.pushToTalk {
                        run([self.config.agentDoctorBin, "dictate", "start"])
                    } else {
                        run([self.config.agentDoctorBin, "dictate", "toggle"])
                    }
                }
            } else if ev.type == .keyUp {
                self.keyDown = false
                if self.config.pushToTalk {
                    run([self.config.agentDoctorBin, "dictate", "stop"])
                }
            }
        }
    }
}

let daemon = HotkeyDaemon()
daemon.reload()

signal(SIGHUP) { _ in
    DispatchQueue.main.async { daemon.reload() }
}
signal(SIGTERM) { _ in exit(0) }

NSApplication.shared.run()
```

- [ ] **Step 2: Update `pyproject.toml` to ship the Swift source**

In `pyproject.toml`, change the `package-data` line:

```toml
[tool.setuptools.package-data]
agent_doctor = ["assets/*.png", "assets/*.swift", "hotkey/*.swift"]
```

- [ ] **Step 3: Commit**

```bash
git add agent_doctor/hotkey/HotkeyHelper.swift pyproject.toml
git commit -m "feat(hotkey): bundle Swift global-hotkey helper source"
```

---

## Task 3: `hotkey_install.py` — build, plist, SIGHUP, uninstall

**Files:**
- Create: `agent_doctor/hotkey_install.py`
- Test: `tests/test_hotkey_install.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hotkey_install.py`:

```python
"""Tests for hotkey_install build / plist / launchctl wiring."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import hotkey_install as hi


def _make_fake_swiftc(tmp_path: Path) -> Path:
    """Write a fake swiftc script that just writes a dummy binary to -o target."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "swiftc"
    fake.write_text(
        '#!/usr/bin/env bash\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then\n'
        '    shift\n'
        '    echo "#!/bin/sh\\necho ran helper $@" > "$1"\n'
        '    chmod +x "$1"\n'
        '    shift\n'
        '    continue\n'
        '  fi\n'
        '  shift\n'
        'done\n'
        'exit 0\n'
    )
    fake.chmod(0o755)
    return bin_dir


def test_build_runs_swiftc_and_writes_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{':' + str(tmp_path)}")
    src = tmp_path / "HotkeyHelper.swift"
    src.write_text("// fake source")
    dest = tmp_path / "out" / "agent-doctor-hotkey"
    hi.build(src, dest)
    assert dest.exists()
    assert dest.stat().st_mode & 0o111  # executable


def test_build_raises_without_swiftc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no swiftc on PATH
    src = tmp_path / "HotkeyHelper.swift"
    src.write_text("// fake")
    with pytest.raises(hi.HotkeyInstallError, match="swiftc"):
        hi.build(src, tmp_path / "out" / "bin")


def test_write_plist_content(tmp_path: Path) -> None:
    plist_path = tmp_path / "com.agent-doctor.hotkey.plist"
    helper = tmp_path / "helper"
    helper.write_text("# fake")
    helper.chmod(0o755)
    hi.write_plist(plist_path, helper, "/usr/local/bin/agent-doctor")
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == "com.agent-doctor.hotkey"
    assert parsed["ProgramArguments"] == [str(helper)]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["EnvironmentVariables"]["AGENT_DOCTOR_BIN"] == "/usr/local/bin/agent-doctor"


def test_install_calls_launchctl_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "helper")
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    monkeypatch.setattr(hi, "SWIFT_SOURCE", tmp_path / "HotkeyHelper.swift")
    (tmp_path / "HotkeyHelper.swift").write_text("// fake source")

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(hi, "_run_launchctl", fake_run)
    hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert any(arg[:2] == ["launchctl", "bootstrap"] for arg in calls)


def test_uninstall_calls_launchctl_bootout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "helper")
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    (tmp_path / "plist").write_text("<plist/>")

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(hi, "_run_launchctl", fake_run)
    hi.uninstall()
    assert any(arg[:2] == ["launchctl", "bootout"] for arg in calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hotkey_install.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `hotkey_install.py`**

Create `agent_doctor/hotkey_install.py`:

```python
"""Build the Swift hotkey helper and register it with launchd.

Public API:
- ``build(src, dest)`` — compile the Swift source.
- ``write_plist(path, helper, agent_doctor_bin)`` — produce the LaunchAgent plist.
- ``install(agent_doctor_bin=None)`` — build, write plist, launchctl bootstrap.
- ``sighup()`` — kick the running daemon so it re-reads ``dictate.json``.
- ``uninstall()`` — launchctl bootout + remove the plist.

All shell-outs go through ``_run_launchctl`` so tests can stub them.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

LABEL = "com.agent-doctor.hotkey"
DEFAULT_HELPER_PATH = Path("~/Library/Application Support/agent-doctor/bin/agent-doctor-hotkey").expanduser()
DEFAULT_PLIST_PATH = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
SWIFT_SOURCE = Path(__file__).with_name("hotkey") / "HotkeyHelper.swift"


class HotkeyInstallError(RuntimeError):
    pass


def build(src: Path, dest: Path) -> Path:
    if shutil.which("swiftc") is None:
        raise HotkeyInstallError(
            "swiftc not found on PATH; install Xcode Command Line Tools with "
            "'xcode-select --install'"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", "-O", str(src), "-o", str(dest)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"swiftc failed (rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace')}"
        )
    dest.chmod(0o755)
    return dest


def write_plist(path: Path, helper: Path, agent_doctor_bin: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(helper)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "AGENT_DOCTOR_BIN": agent_doctor_bin,
            # macOS does not inherit PATH for launchd-managed processes; bake one.
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
        "StandardOutPath": str(Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()),
        "StandardErrorPath": str(Path("~/Library/Logs/agent-doctor-hotkey.err.log").expanduser()),
    }
    body = plistlib.dumps(payload)
    path.write_bytes(body)
    return path


def _run_launchctl(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, check=check)


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def install(*, agent_doctor_bin: Optional[str] = None) -> dict[str, str]:
    helper = DEFAULT_HELPER_PATH
    plist = DEFAULT_PLIST_PATH
    bin_path = agent_doctor_bin or shutil.which("agent-doctor") or "/usr/local/bin/agent-doctor"
    build(SWIFT_SOURCE, helper)
    write_plist(plist, helper, bin_path)
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    proc = _run_launchctl(["launchctl", "bootstrap", _domain_target(), str(plist)])
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"launchctl bootstrap failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return {"helper": str(helper), "plist": str(plist), "agent_doctor_bin": bin_path}


def sighup() -> bool:
    """Send SIGHUP to the running daemon. Returns True on success."""

    proc = _run_launchctl(["launchctl", "kill", "SIGHUP", f"{_domain_target()}/{LABEL}"])
    return proc.returncode == 0


def uninstall() -> dict[str, str]:
    plist = DEFAULT_PLIST_PATH
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    plist.unlink(missing_ok=True)
    return {"plist_removed": str(plist)}


def status() -> dict[str, object]:
    plist_exists = DEFAULT_PLIST_PATH.exists()
    helper_exists = DEFAULT_HELPER_PATH.exists()
    proc = _run_launchctl(["launchctl", "print", f"{_domain_target()}/{LABEL}"])
    running = proc.returncode == 0
    return {
        "plist": str(DEFAULT_PLIST_PATH),
        "plist_exists": plist_exists,
        "helper": str(DEFAULT_HELPER_PATH),
        "helper_exists": helper_exists,
        "running": running,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hotkey_install.py -v`
Expected: 5 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/hotkey_install.py tests/test_hotkey_install.py
git commit -m "feat(hotkey): build helper + launchd plist + bootstrap/uninstall"
```

---

## Task 4: Wire CLI subcommands `dictate hotkey …`

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_cli_subcommand_registration.py`

- [ ] **Step 1: Add CLI registration**

In `agent_doctor/cli.py`, after the `dictate llm` block from Phase 2 Task 6, append:

```python
    dictate_hotkey = dictate_subs.add_parser(
        "hotkey",
        help="Configure the global push-to-talk hotkey daemon (macOS).",
    )
    dictate_hotkey_subs = dictate_hotkey.add_subparsers(
        dest="dictate_hotkey_cmd", required=True
    )

    hk_install = dictate_hotkey_subs.add_parser(
        "install", help="Compile the Swift helper and load the LaunchAgent."
    )
    hk_install.add_argument(
        "--agent-doctor-bin",
        default=None,
        help="Path to the agent-doctor executable (defaults to `which agent-doctor`).",
    )
    hk_install.set_defaults(func=_cmd_dictate_hotkey_install)

    hk_set = dictate_hotkey_subs.add_parser(
        "set", help="Update the chord and/or push-to-talk mode."
    )
    hk_set.add_argument("binding", help="e.g. 'ctrl+option+space'.")
    hk_set.add_argument(
        "--push-to-talk",
        dest="push_to_talk",
        action="store_true",
        default=None,
        help="Force push-to-talk mode (hold to record).",
    )
    hk_set.add_argument(
        "--toggle",
        dest="push_to_talk",
        action="store_false",
        help="Force toggle mode (single press).",
    )
    hk_set.set_defaults(func=_cmd_dictate_hotkey_set)

    hk_show = dictate_hotkey_subs.add_parser(
        "show", help="Show binding + daemon state."
    )
    hk_show.add_argument("--json", action="store_true")
    hk_show.set_defaults(func=_cmd_dictate_hotkey_show)

    hk_test = dictate_hotkey_subs.add_parser(
        "test", help="Capture the next chord pressed and print its canonical form."
    )
    hk_test.add_argument(
        "--seconds", type=float, default=5.0, help="Listen window (default 5s)."
    )
    hk_test.set_defaults(func=_cmd_dictate_hotkey_test)

    hk_uninstall = dictate_hotkey_subs.add_parser(
        "uninstall", help="Unload and remove the LaunchAgent."
    )
    hk_uninstall.set_defaults(func=_cmd_dictate_hotkey_uninstall)
```

Add handlers at the end of `cli.py`:

```python
def _cmd_dictate_hotkey_install(args: argparse.Namespace) -> int:
    from . import hotkey_install as _hi

    try:
        result = _hi.install(agent_doctor_bin=args.agent_doctor_bin)
    except _hi.HotkeyInstallError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        "\nNext: grant 'Input Monitoring' permission.\n"
        "  open 'x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent'\n"
        "After granting, the daemon picks it up automatically (LaunchAgent KeepAlive=true).",
        file=sys.stderr,
    )
    return 0


def _cmd_dictate_hotkey_set(args: argparse.Namespace) -> int:
    from . import dictate_settings as _ds
    from . import hotkey_install as _hi
    from . import hotkey_parse as _hp

    try:
        chord = _hp.parse(args.binding)
    except _hp.HotkeyParseError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2

    settings = _ds.load()
    new = _ds.HotkeySettings(
        binding=chord.canonical(),
        push_to_talk=settings.hotkey.push_to_talk if args.push_to_talk is None else bool(args.push_to_talk),
        daemon_enabled=settings.hotkey.daemon_enabled,
    )
    _ds.save(_ds.replace_section(settings, hotkey=new))
    if _hi.DEFAULT_PLIST_PATH.exists():
        _hi.sighup()
    print(f"binding: {new.binding}\npush_to_talk: {new.push_to_talk}")
    return 0


def _cmd_dictate_hotkey_show(args: argparse.Namespace) -> int:
    from . import dictate_settings as _ds
    from . import hotkey_install as _hi

    settings = _ds.load()
    status = _hi.status()
    payload = {
        "binding": settings.hotkey.binding,
        "push_to_talk": settings.hotkey.push_to_talk,
        "daemon_enabled": settings.hotkey.daemon_enabled,
        **status,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for k, v in payload.items():
        print(f"{k}: {v}")
    return 0


def _cmd_dictate_hotkey_test(args: argparse.Namespace) -> int:
    print(
        "agent-doctor: 'hotkey test' is a UI-only smoke check.\n"
        "  Open the Preferences window or watch the daemon log at\n"
        f"  ~/Library/Logs/agent-doctor-hotkey.log to verify the chord.",
        file=sys.stderr,
    )
    return 0


def _cmd_dictate_hotkey_uninstall(_args: argparse.Namespace) -> int:
    from . import hotkey_install as _hi

    result = _hi.uninstall()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
```

- [ ] **Step 2: Update the registration smoke test**

Append to `tests/test_cli_subcommand_registration.py`:

```python
EXPECTED_DICTATE_HOTKEY_SUBCOMMANDS = {"install", "set", "show", "test", "uninstall"}


def test_dictate_hotkey_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    hk_sub = _nested_subparser(dictate_sub, "hotkey")
    assert _subparser_choices(hk_sub) >= EXPECTED_DICTATE_HOTKEY_SUBCOMMANDS
```

- [ ] **Step 3: Run the tests**

Run: `python3 -m pytest tests/test_cli_subcommand_registration.py tests/test_hotkey_install.py tests/test_hotkey_parse.py -v`
Expected: all green.

- [ ] **Step 4: Hand-run a smoke check**

```bash
python3 -m agent_doctor.cli dictate hotkey show
python3 -m agent_doctor.cli dictate hotkey set "ctrl+option+space"
```

Expected: `show` prints current binding and "not installed" status; `set` updates settings and exits 0.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/cli.py tests/test_cli_subcommand_registration.py
git commit -m "feat(cli): register dictate hotkey {install,set,show,test,uninstall}"
```

---

## Task 5: README + manual install / uninstall smoke

- [ ] **Step 1: Document the hotkey commands**

In `README.md`:

```markdown
### Global hotkey (macOS)

```bash
# One-time install: compiles the Swift helper and registers a LaunchAgent.
agent-doctor dictate hotkey install

# Change the chord.
agent-doctor dictate hotkey set "ctrl+option+space"

# Show status + binding.
agent-doctor dictate hotkey show

# Stop and remove the daemon.
agent-doctor dictate hotkey uninstall
```

The helper is a ~150 LOC Swift binary compiled with `swiftc` (requires Xcode Command Line Tools). It reads `~/.agent-doctor/dictate.json` on launch and on SIGHUP, so `set` updates take effect without re-installing.

You will be prompted to grant **Input Monitoring** in System Settings -> Privacy & Security the first time the daemon registers an `NSEvent` monitor.
```

- [ ] **Step 2: Manual install smoke (if you have Xcode CLT)**

```bash
agent-doctor dictate hotkey install
launchctl print gui/$(id -u)/com.agent-doctor.hotkey | head -20  # should be running
```

Hold the bound chord and confirm `agent-doctor dictate start` fires. Press again to stop.

- [ ] **Step 3: Manual uninstall**

```bash
agent-doctor dictate hotkey uninstall
launchctl print gui/$(id -u)/com.agent-doctor.hotkey  # should report unknown
```

- [ ] **Step 4: Commit + tag**

```bash
git add README.md
git commit -m "docs(hotkey): document install + chord set commands"
git tag dictate-phase-4-complete
```

---

## Phase 4 verification checklist

- [ ] `python3 -m pytest -q -m "not tkinter"` is green.
- [ ] `agent-doctor dictate hotkey show` reports current binding + daemon status.
- [ ] On a machine with `swiftc`, `agent-doctor dictate hotkey install` builds the helper and `launchctl print` shows it running.
- [ ] Pressing the bound chord triggers `dictate start` (PTT) or `dictate toggle`.
- [ ] `agent-doctor dictate hotkey set "<chord>"` reloads the daemon via SIGHUP without re-install.
- [ ] `agent-doctor dictate hotkey uninstall` removes the plist and stops the daemon.
- [ ] No new runtime dependencies introduced.
