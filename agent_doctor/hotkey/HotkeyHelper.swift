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
