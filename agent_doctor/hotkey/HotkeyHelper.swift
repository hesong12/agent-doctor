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

func touchHeartbeat() {
    let now = Date()
    if let data = "\(Int(now.timeIntervalSince1970))\n".data(using: .utf8) {
        try? data.write(to: HEARTBEAT_PATH, options: .atomic)
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
        monitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] ev in
            touchHeartbeat()
            guard let self = self else { return }
            let flags = ev.modifierFlags.intersection(.deviceIndependentFlagsMask)
            let myFlagOn = flags.contains(myFlag)
            var others: NSEvent.ModifierFlags = [.command, .option, .control, .shift, .function]
            others.remove(myFlag)
            let anyOther = !flags.intersection(others).isEmpty

            // Start requires the event to originate from the BOUND physical
            // key (so left_cmd doesn't accidentally trigger a right_cmd
            // binding). Stop must fire on ANY flagsChanged event that
            // invalidates the alone-key state — including events from other
            // modifier keys, otherwise "user pressed Shift while holding the
            // bound modifier" would never release.
            let isOurKey = ev.keyCode == keyCode

            if myFlagOn && !anyOther && isOurKey {
                if !self.keyDown {
                    self.keyDown = true
                    run([self.config.agentDoctorBin, "dictate", "start"])
                }
            } else if self.keyDown {
                self.keyDown = false
                run([self.config.agentDoctorBin, "dictate", "stop"])
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
