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
        binding: "right_cmd",
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
    // Modifier-only keycodes — used when the binding is a single modifier
    // held to dictate (Handy-style). See HotkeyDaemon.installFlagsMonitor.
    "left_cmd": 55, "right_cmd": 54,
    "left_option": 58, "right_option": 61,
    "left_ctrl": 59, "right_ctrl": 62,
    "left_shift": 56, "right_shift": 60,
    "fn": 63,
]

let MODIFIER_ONLY_KEYCODES: Set<UInt16> = [54, 55, 56, 58, 59, 60, 61, 62, 63]

// Map each modifier-only keycode to the corresponding flag bit so the
// daemon can confirm "this physical key produced this flag change."
let MODIFIER_FLAG_FOR_KEYCODE: [UInt16: NSEvent.ModifierFlags] = [
    54: .command, 55: .command,
    58: .option,  61: .option,
    59: .control, 62: .control,
    56: .shift,   60: .shift,
    63: .function,
]

struct ParsedChord {
    var modifiers: NSEvent.ModifierFlags
    var keyCode: UInt16
}

func parse(_ binding: String) -> ParsedChord? {
    let tokens = binding.lowercased().split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
    if tokens.isEmpty { return nil }

    // Reject any binding that mixes a modifier-only token with anything
    // else (matches the Python parser's contract). Without this guard a
    // typo like "right_cmd+space" silently registers Space-alone as the
    // global hotkey, because the modifier-only keycode would be overwritten
    // by the trailing "space" lookup below.
    let modifierOnlyTokens: Set<String> = [
        "left_cmd", "right_cmd",
        "left_option", "right_option",
        "left_ctrl", "right_ctrl",
        "left_shift", "right_shift",
        "fn",
    ]
    let modOnlyHits = tokens.filter { modifierOnlyTokens.contains($0) }
    if !modOnlyHits.isEmpty && tokens.count != 1 {
        fputs("hotkey: modifier-only binding \(modOnlyHits[0]) cannot mix with other tokens (got \(binding))\n", stderr)
        return nil
    }

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
    // Inherit the helper's own stdout/stderr instead of black-holing
    // the child's output to /dev/null. The launchd plist redirects
    // those file descriptors to
    // ~/Library/Logs/agent-doctor-hotkey{,.err}.log, so any stderr
    // line from `agent-doctor dictate start/stop` (whisper.cpp init
    // logs, paste osascript exit codes, traceback on failure) lands
    // in a file we can read after the fact. Silent failures were the
    // root cause of multiple "doesn't work" reports during PR #44/#45
    // smoke — there was no on-disk evidence whatsoever.
    do { try proc.run() } catch {
        fputs("hotkey: failed to launch \(argv): \(error)\n", stderr)
    }
}

// Heartbeat file. The helper rewrites this on every global event so the
// Python side can infer whether Input Monitoring is granted: if the helper
// is registered but the heartbeat is missing or stale, IM is almost
// certainly revoked (or events aren't reaching us for some other reason).
let HEARTBEAT_PATH: URL = {
    let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
    let dir = appSupport.appendingPathComponent("agent-doctor")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("im-heartbeat")
}()

// Startup stamp. Written once before the event monitor is installed. The
// Python probe compares (heartbeat mtime) > (startup mtime) to decide if
// genuine events have flowed since this daemon started — eliminates the
// false positive where heartbeat from a previous daemon lifetime makes
// IM look granted when it has been revoked since.
let STARTUP_STAMP_PATH: URL = {
    let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
    let dir = appSupport.appendingPathComponent("agent-doctor")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("im-startup")
}()

func writeStartupStamp() {
    let now = Date().timeIntervalSince1970
    if let data = "\(Int(now))\n".data(using: .utf8) {
        try? data.write(to: STARTUP_STAMP_PATH, options: .atomic)
    }
}

// Paste signal file. Python's `dictate stop` writes this after the
// optimized prompt lands on the clipboard. The helper polls for it
// and, when present, synthesises Cmd+V via CGEventPost — which goes
// through the helper's own Accessibility grant rather than through
// osascript's (which is unreachable from a launchd-spawned process
// chain, error -1002).
let PASTE_REQUEST_PATH: URL = {
    let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
    let dir = appSupport.appendingPathComponent("agent-doctor")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("paste-request")
}()

// V on the US ANSI layout. Same keycode the AppleScript shim used.
let KEYCODE_V: CGKeyCode = 9

// Left Command physical keycode. Matches what enigo (the Rust crate
// Handy uses) sends for Key::Meta on macOS. Posting key events for
// this code rather than `.flagsChanged` events is what makes Electron
// apps (Cursor, VS Code, Slack, Discord) actually honour the
// synthesised ⌘V — they treat flagsChanged as decorative state but
// only react to real key events with the Cmd modifier bit set.
let KEYCODE_LEFT_CMD: CGKeyCode = 55

// Undocumented but mandatory flag bit. enigo (and reverse-engineered
// macOS event traces) sets this on every synthesised CGEvent because
// "correct events have it set" — without it, Electron / Chromium apps
// silently ignore the keystroke. Suspected name is something like
// kCGEventFlagDeviceTrusted; the constant doesn't appear in Apple's
// public headers but apps that consume CGEvents (Cursor, VS Code,
// Slack) check it before honouring the modifier mask.
//
// See enigo's src/macos/macos_impl.rs around line 537:
//     event_flags.set(CGEventFlags::from_bits_retain(0x2000_0000), true);
//     // "I don't know if this is needed or what this flag does.
//     //  Correct events have it set so we also do it"
let CGEVENT_TRUSTED_BIT: UInt64 = 0x2000_0000

func sendCmdV() {
    // Sequence patterned on enigo's macOS impl (cf. Handy's
    // src-tauri/src/input.rs send_paste_ctrl_v):
    //   1. Cmd key DOWN
    //   2. V key DOWN with cmd flag
    //   3. V key UP with cmd flag
    //   4. 100 ms wait (apps' run-loops process the press)
    //   5. Cmd key UP
    // The earlier flagsChanged + V approach posted to the cghid tap
    // successfully but Cursor never saw a paste — Electron's keyboard
    // bridge appears to require literal key events for the modifier,
    // not flag transitions.
    let source = CGEventSource(stateID: .combinedSessionState)
    fputs("hotkey: sendCmdV source=\(source != nil)\n", stderr)
    // Helper closure: set the trusted bit + maskCommand on flags as
    // appropriate, then post. Centralised so we can't forget one path.
    func postKey(_ keyCode: CGKeyCode, _ down: Bool, _ withCmdFlag: Bool) {
        guard let ev = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: down) else {
            fputs("hotkey: failed to create CGEvent kc=\(keyCode) down=\(down)\n", stderr)
            return
        }
        var flagBits: UInt64 = CGEVENT_TRUSTED_BIT
        if withCmdFlag {
            flagBits |= CGEventFlags.maskCommand.rawValue
        }
        ev.flags = CGEventFlags(rawValue: flagBits)
        ev.post(tap: .cgSessionEventTap)
    }
    postKey(KEYCODE_LEFT_CMD, true, true)
    fputs("hotkey: posted Cmd keyDown\n", stderr)
    postKey(KEYCODE_V, true, true)
    fputs("hotkey: posted V keyDown\n", stderr)
    postKey(KEYCODE_V, false, true)
    fputs("hotkey: posted V keyUp\n", stderr)
    // Match enigo's 100 ms pause before releasing the modifier so the
    // target app has time to dispatch the V before the Cmd flag clears.
    usleep(100_000)
    postKey(KEYCODE_LEFT_CMD, false, false)
    fputs("hotkey: posted Cmd keyUp\n", stderr)
}

func startPasteRequestWatcher() {
    // Polling cadence: 80 ms. Tradeoffs: 50 ms drains battery on idle
    // hours; 200 ms feels laggy after a 5-10 s pipeline. 80 ms gives a
    // perceived ~instant response with negligible CPU.
    Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { _ in
        let path = PASTE_REQUEST_PATH.path
        if !FileManager.default.fileExists(atPath: path) { return }
        // Remove first so any concurrent retry doesn't race us into
        // double-pasting.
        try? FileManager.default.removeItem(atPath: path)
        sendCmdV()
    }
}

// Minimum interval between heartbeat writes (seconds). The probe needs
// freshness within 60s, so writing every 5s gives plenty of margin while
// avoiding per-keystroke disk I/O on chord bindings whose monitor sees
// every keyDown/keyUp in the system.
let HEARTBEAT_MIN_INTERVAL_S: TimeInterval = 5.0

// MARK: - Heartbeat (main-thread only)
var lastHeartbeatAt: TimeInterval = 0

func touchHeartbeat() {
    let now = Date().timeIntervalSince1970
    if now - lastHeartbeatAt < HEARTBEAT_MIN_INTERVAL_S {
        return
    }
    lastHeartbeatAt = now
    if let data = "\(Int(now))\n".data(using: .utf8) {
        try? data.write(to: HEARTBEAT_PATH, options: .atomic)
    }
}

class HotkeyDaemon {
    var config: Config = readConfig()
    var chord: ParsedChord? = nil
    var monitor: Any? = nil
    var keyDown = false
    // State of the bound modifier flag before the current event. Used to
    // distinguish "user just pressed the bound physical key" from "user
    // pressed the other physical key of the same modifier-type (e.g. left
    // Cmd) while the flag is already on."
    var lastFlagOn = false
    // Pending delayed `dictate start` work item for modifier-only bindings.
    // We defer `start` by ~150ms so we can observe whether a non-modifier
    // keyDown follows (in which case the user is doing a system shortcut,
    // not dictating). This avoids the start/cancel race that left a stale
    // recording active when cancel arrived before start had written state.
    var pendingStartWork: DispatchWorkItem? = nil

    func reload() {
        config = readConfig()
        chord = parse(config.binding)
        if let m = monitor {
            NSEvent.removeMonitor(m)
            monitor = nil
        }
        keyDown = false
        lastFlagOn = false
        pendingStartWork?.cancel()
        pendingStartWork = nil
        guard let c = chord else {
            fputs("hotkey: could not parse binding \(config.binding)\n", stderr)
            return
        }
        if c.modifiers.isEmpty && MODIFIER_ONLY_KEYCODES.contains(c.keyCode) {
            installFlagsMonitor(for: c.keyCode)
        } else {
            installKeyMonitor(for: c)
        }
    }

    private func installKeyMonitor(for c: ParsedChord) {
        let mask: NSEvent.EventTypeMask = [.keyDown, .keyUp]
        monitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] ev in
            touchHeartbeat()
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

    private func installFlagsMonitor(for keyCode: UInt16) {
        guard let myFlag = MODIFIER_FLAG_FOR_KEYCODE[keyCode] else { return }
        // We need BOTH event streams: .flagsChanged for the bound modifier
        // press/release, and .keyDown so that a non-modifier key arriving
        // while we're recording (= a system shortcut, not dictation) can
        // cancel the recording before it captures audio of the shortcut.
        let mask: NSEvent.EventTypeMask = [.flagsChanged, .keyDown]
        monitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] ev in
            touchHeartbeat()
            guard let self = self else { return }

            if ev.type == .keyDown {
                // Non-modifier key arrived. If a start is pending (within
                // the 150ms intent-confirmation window), the user is doing
                // a shortcut, not dictating — cancel before it ever fires.
                // This avoids the start/cancel race where cancel could
                // observe "no recording" and no-op before start wrote state.
                if let pending = self.pendingStartWork {
                    pending.cancel()
                    self.pendingStartWork = nil
                }
                // If we already started, kill the recording.
                if self.keyDown {
                    self.keyDown = false
                    run([self.config.agentDoctorBin, "dictate", "cancel"])
                }
                return
            }
            // .flagsChanged from here on.
            let flags = ev.modifierFlags.intersection(.deviceIndependentFlagsMask)
            let myFlagOn = flags.contains(myFlag)
            var others: NSEvent.ModifierFlags = [.command, .option, .control, .shift, .function]
            others.remove(myFlag)
            let anyOther = !flags.intersection(others).isEmpty
            let isOurKey = ev.keyCode == keyCode

            let wasOn = self.lastFlagOn
            self.lastFlagOn = myFlagOn

            if isOurKey && myFlagOn && !anyOther && !wasOn {
                // Bound key just pressed alone. Schedule a delayed start —
                // cancelled if a non-modifier key follows within 150ms (=
                // system shortcut) or the key is released within 150ms (=
                // accidental tap).
                let work = DispatchWorkItem { [weak self] in
                    guard let self = self else { return }
                    self.pendingStartWork = nil
                    if !self.keyDown {
                        self.keyDown = true
                        run([self.config.agentDoctorBin, "dictate", "start"])
                    }
                }
                self.pendingStartWork?.cancel()
                self.pendingStartWork = work
                DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(150), execute: work)
                return
            }

            // Same-modifier release: recording is active and the bound
            // key fired flagsChanged again while its flag bit is still on
            // (e.g. user held right_cmd, then released it while pressing
            // left_cmd — the cmd flag stays on but the bound-side keycode
            // changed). Treat that as a release and stop dictation.
            if isOurKey && myFlagOn && !anyOther && wasOn && self.keyDown {
                self.keyDown = false
                run([self.config.agentDoctorBin, "dictate", "stop"])
                return
            }

            // Stop branch — recording is active but the bound flag dropped
            // or another modifier intruded.
            if self.keyDown && (!myFlagOn || anyOther) {
                self.keyDown = false
                run([self.config.agentDoctorBin, "dictate", "stop"])
                return
            }

            // If the bound key was released within the intent-confirmation
            // window (< 150ms tap), cancel the pending start entirely —
            // neither start nor stop fires.
            if isOurKey && !myFlagOn && self.pendingStartWork != nil {
                self.pendingStartWork?.cancel()
                self.pendingStartWork = nil
            }
        }
    }
}

// Stamp startup before installing the event monitor, so the Python probe
// can distinguish "events have flowed since *this* daemon started"
// (= IM granted) from "heartbeat from a previous lifetime still on disk"
// (= IM might be revoked since).
writeStartupStamp()

// Paste request watcher MOVED to pet_display.swift. macOS silently
// rejects keystroke synthesis from launchd-spawned process contexts
// (verified empirically: the same CGEventPost code works from a
// Terminal-spawned CLI Swift script but no-ops from this helper even
// with Accessibility granted). The pet-display Swift process IS
// spawned by the user's shell, so it handles paste-request signals
// instead. See pet_display.swift's "Paste-request watcher" section.

let daemon = HotkeyDaemon()
daemon.reload()

signal(SIGHUP) { _ in
    DispatchQueue.main.async { daemon.reload() }
}
signal(SIGTERM) { _ in exit(0) }

NSApplication.shared.run()
