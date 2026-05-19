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
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = FileHandle.nullDevice
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

// Startup stamp. Rewritten on every monitor (re)install so the Python probe
// can distinguish "leftover heartbeat from a previous run before IM was
// revoked" from "heartbeat written after the current monitor came up".
// Python requires heartbeat.mtime > startup.mtime; if no events arrive after
// install, the stamp stays newer and the probe returns False — even when an
// old heartbeat is still on disk. Without this, a user who revokes IM and
// relaunches the helper would see a stale "fresh" heartbeat for up to 60s
// and the Preferences pane would falsely report "Listening".
let STARTUP_PATH: URL = {
    let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
    let dir = appSupport.appendingPathComponent("agent-doctor")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("im-startup")
}()

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

// Called exactly once per monitor install (i.e. once per reload). The Python
// probe compares heartbeat.mtime > startup.mtime so any heartbeat written
// before this call is treated as stale. Reset lastHeartbeatAt too so the
// first event after install always writes a fresh heartbeat regardless of
// how recently the previous run wrote one — without this, a SIGHUP-driven
// reload within 5s of the previous heartbeat would silently swallow the
// next event's update and leave the probe seeing a pre-install heartbeat.
func touchStartupStamp() {
    let now = Date().timeIntervalSince1970
    lastHeartbeatAt = 0
    if let data = "\(Int(now))\n".data(using: .utf8) {
        try? data.write(to: STARTUP_PATH, options: .atomic)
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
        // Stamp the startup file BEFORE installing the monitor. The Python
        // probe requires heartbeat.mtime > startup.mtime, so any leftover
        // heartbeat from a previous run (or from before IM was revoked) is
        // automatically classified as stale until the new monitor produces
        // its first event.
        touchStartupStamp()
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

let daemon = HotkeyDaemon()
daemon.reload()

signal(SIGHUP) { _ in
    DispatchQueue.main.async { daemon.reload() }
}
signal(SIGTERM) { _ in exit(0) }

NSApplication.shared.run()
