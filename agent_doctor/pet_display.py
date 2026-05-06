"""Desktop display for Doctor Pet.

This module is intentionally optional UI glue. It lazy-imports ``tkinter`` so
the scan/apply/autopilot production path remains dependency-free and headless
safe. The display reads ``pet-status.json`` and renders a small always-on-top
doctor pet window that refreshes as autopilot updates the file.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir
from typing import Any


@dataclass(frozen=True)
class DisplaySnapshot:
    state: str
    action: str
    severity: str
    headline: str
    message: str
    session_id: str
    fill: str
    accent: str


def default_status_file() -> Path:
    return Path("~/.agent-doctor/pet/pet-status.json").expanduser()


def read_status_payload(status_file: Path) -> dict[str, Any]:
    path = status_file.expanduser()
    if not path.exists():
        return {
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "headline": "Doctor Pet is waiting for Agent Doctor status.",
            "message": f"Status file not found yet: {path}",
            "session_id": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "state": "concerned",
            "action": "notify",
            "severity": "medium",
            "headline": "Doctor Pet could not read its status file.",
            "message": str(exc),
            "session_id": "",
        }
    if not isinstance(data, dict):
        return {
            "state": "concerned",
            "action": "notify",
            "severity": "medium",
            "headline": "Doctor Pet status file has the wrong shape.",
            "message": "Expected a JSON object.",
            "session_id": "",
        }
    return data


def snapshot_from_payload(payload: dict[str, Any]) -> DisplaySnapshot:
    state = str(payload.get("state") or "idle")
    action = str(payload.get("action") or "silent")
    severity = str(payload.get("severity") or "low")
    if state == "intervening":
        fill = "#f8d3d0"
        accent = "#b42318"
    elif state == "concerned":
        fill = "#fdecc8"
        accent = "#b54708"
    elif state == "watching":
        fill = "#d8e8ff"
        accent = "#175cd3"
    else:
        fill = "#e7f0ff"
        accent = "#3556c7"
    return DisplaySnapshot(
        state=state,
        action=action,
        severity=severity,
        headline=str(payload.get("headline") or "Doctor Pet is idle."),
        message=str(payload.get("message") or ""),
        session_id=str(payload.get("session_id") or ""),
        fill=fill,
        accent=accent,
    )


def display_pet(
    status_file: Path | None = None,
    *,
    poll_seconds: float = 1.0,
    topmost: bool = True,
) -> None:
    """Open an always-on-top Doctor Pet window and refresh from status JSON."""

    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - environment-specific
        if platform.system() == "Darwin" and shutil.which("swift"):
            _display_pet_appkit(
                status_file or default_status_file(),
                poll_seconds=poll_seconds,
                topmost=topmost,
            )
            return
        raise RuntimeError(
            "Doctor Pet desktop display requires tkinter, or Swift/AppKit on macOS. "
            "Use `agent-doctor pet --out <dir>` to render files without a desktop window."
        ) from exc

    status_path = (status_file or default_status_file()).expanduser()
    interval_ms = max(200, int(poll_seconds * 1000))

    root = tk.Tk()
    root.title("Agent Doctor Pet")
    root.geometry("180x210+120+120")
    root.resizable(False, False)
    root.overrideredirect(True)
    root.configure(bg="#ff00ff")
    if topmost:
        root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", "#ff00ff")
    except Exception:
        pass

    canvas = tk.Canvas(root, width=180, height=210, bg="#ff00ff", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    drag = {"x": 0, "y": 0}

    def start_drag(event: Any) -> None:
        drag["x"] = event.x
        drag["y"] = event.y

    def move_drag(event: Any) -> None:
        root.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

    canvas.bind("<ButtonPress-1>", start_drag)
    canvas.bind("<B1-Motion>", move_drag)

    def draw() -> None:
        snapshot = snapshot_from_payload(read_status_payload(status_path))
        canvas.delete("all")
        _draw_pet(canvas, snapshot)
        canvas.after(interval_ms, draw)

    draw()
    root.mainloop()


def _draw_pet(canvas: Any, snapshot: DisplaySnapshot) -> None:
    canvas.create_rectangle(0, 0, 180, 210, fill="#ff00ff", outline="")

    # Soft shadow.
    canvas.create_oval(54, 178, 128, 196, fill="#1f2937", outline="", stipple="gray50")

    # Cloud hair / doctor cap silhouette.
    canvas.create_oval(36, 28, 90, 88, fill="#88aaff", outline="#111827", width=2)
    canvas.create_oval(74, 22, 136, 88, fill="#9bb8ff", outline="#111827", width=2)
    canvas.create_oval(24, 55, 74, 110, fill="#6f90ed", outline="#111827", width=2)
    canvas.create_oval(120, 58, 162, 108, fill="#7698f2", outline="#111827", width=2)
    canvas.create_rectangle(68, 40, 114, 58, fill="#ffffff", outline="#111827", width=2)
    canvas.create_text(91, 49, text="+", fill=snapshot.accent, font=("Helvetica", 14, "bold"))

    # Face screen.
    canvas.create_rectangle(50, 72, 132, 119, fill="#172554", outline="#111827", width=3)
    eye_color = "#93c5fd" if snapshot.state != "intervening" else "#ffffff"
    canvas.create_arc(70, 90, 86, 105, start=200, extent=140, style="arc", outline=eye_color, width=3)
    canvas.create_arc(100, 90, 116, 105, start=200, extent=140, style="arc", outline=eye_color, width=3)

    # Body / coat.
    canvas.create_rectangle(64, 128, 118, 178, fill=snapshot.fill, outline="#111827", width=3)
    canvas.create_line(91, 130, 91, 177, fill="#cbd5e1", width=2)
    canvas.create_line(76, 140, 89, 153, fill="#cbd5e1", width=2)
    canvas.create_line(106, 140, 93, 153, fill="#cbd5e1", width=2)
    canvas.create_oval(79, 145, 103, 169, outline=snapshot.accent, width=3)
    canvas.create_line(91, 169, 91, 177, fill=snapshot.accent, width=3)

    # Arms / legs.
    canvas.create_line(63, 140, 42, 166, fill="#5b7ee5", width=11)
    canvas.create_line(119, 140, 140, 166, fill="#5b7ee5", width=11)
    canvas.create_line(78, 178, 74, 199, fill="#5b7ee5", width=12)
    canvas.create_line(104, 178, 108, 199, fill="#5b7ee5", width=12)

    if snapshot.state == "intervening":
        canvas.create_oval(136, 26, 160, 50, fill=snapshot.accent, outline="#ffffff", width=2)
        canvas.create_text(148, 38, text="!", fill="#ffffff", font=("Helvetica", 15, "bold"))


def _shorten(value: str, limit: int = 24) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def _display_pet_appkit(
    status_file: Path,
    *,
    poll_seconds: float,
    topmost: bool,
) -> None:
    source_path = Path(gettempdir()) / "agent_doctor_pet_display.swift"
    source_path.write_text(_appkit_source(), encoding="utf-8")
    command = [
        "swift",
        str(source_path),
        str(status_file.expanduser()),
        str(max(0.2, poll_seconds)),
        "1" if topmost else "0",
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Swift/AppKit Doctor Pet exited with rc={completed.returncode}")


def _appkit_source() -> str:
    return r'''
import Cocoa

let statusPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let pollSeconds = CommandLine.arguments.count > 2 ? (Double(CommandLine.arguments[2]) ?? 1.0) : 1.0
let topmost = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] == "1" : true

func stringValue(_ dict: [String: Any], _ key: String, _ fallback: String) -> String {
    if let value = dict[key] as? String {
        return value
    }
    return fallback
}

func loadStatus() -> [String: String] {
    let url = URL(fileURLWithPath: statusPath)
    guard let data = try? Data(contentsOf: url) else {
        return [
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "headline": "Doctor Pet is waiting for status.",
            "message": "Status file not found yet.",
            "session_id": ""
        ]
    }
    guard
        let obj = try? JSONSerialization.jsonObject(with: data),
        let dict = obj as? [String: Any]
    else {
        return [
            "state": "concerned",
            "action": "notify",
            "severity": "medium",
            "headline": "Doctor Pet could not parse status.",
            "message": "Expected a JSON object.",
            "session_id": ""
        ]
    }
    return [
        "state": stringValue(dict, "state", "idle"),
        "action": stringValue(dict, "action", "silent"),
        "severity": stringValue(dict, "severity", "low"),
        "headline": stringValue(dict, "headline", "Doctor Pet is idle."),
        "message": stringValue(dict, "message", ""),
        "session_id": stringValue(dict, "session_id", "")
    ]
}

func color(_ hex: String) -> NSColor {
    let value = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
    var int: UInt64 = 0
    Scanner(string: value).scanHexInt64(&int)
    let r = CGFloat((int >> 16) & 0xff) / 255.0
    let g = CGFloat((int >> 8) & 0xff) / 255.0
    let b = CGFloat(int & 0xff) / 255.0
    return NSColor(calibratedRed: r, green: g, blue: b, alpha: 1.0)
}

func palette(_ state: String) -> (NSColor, NSColor) {
    if state == "intervening" {
        return (color("#f8d3d0"), color("#b42318"))
    }
    if state == "concerned" {
        return (color("#fdecc8"), color("#b54708"))
    }
    if state == "watching" {
        return (color("#d8e8ff"), color("#175cd3"))
    }
    return (color("#e7f0ff"), color("#3556c7"))
}

class PetView: NSView {
    var status: [String: String] = loadStatus() {
        didSet { needsDisplay = true }
    }
    var dragOffset: NSPoint = .zero

    override var isOpaque: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }

    override func mouseDown(with event: NSEvent) {
        dragOffset = event.locationInWindow
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window = self.window else { return }
        let mouse = NSEvent.mouseLocation
        window.setFrameOrigin(NSPoint(x: mouse.x - dragOffset.x, y: mouse.y - dragOffset.y))
    }

    func r(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {
        return NSRect(x: x, y: bounds.height - y - h, width: w, height: h)
    }

    func text(_ value: String, _ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ size: CGFloat, _ colorValue: NSColor, _ bold: Bool = false, _ align: NSTextAlignment = .center) {
        let style = NSMutableParagraphStyle()
        style.alignment = align
        let font = bold ? NSFont.boldSystemFont(ofSize: size) : NSFont.systemFont(ofSize: size)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: colorValue,
            .paragraphStyle: style
        ]
        NSString(string: value).draw(in: r(x, y, w, h), withAttributes: attrs)
    }

    func oval(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ fill: NSColor, _ stroke: NSColor = color("#111827"), _ width: CGFloat = 2) {
        let path = NSBezierPath(ovalIn: r(x, y, w, h))
        fill.setFill()
        path.fill()
        stroke.setStroke()
        path.lineWidth = width
        path.stroke()
    }

    func roundRect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ radius: CGFloat, _ fill: NSColor, _ stroke: NSColor = color("#111827"), _ width: CGFloat = 2) {
        let path = NSBezierPath(roundedRect: r(x, y, w, h), xRadius: radius, yRadius: radius)
        fill.setFill()
        path.fill()
        stroke.setStroke()
        path.lineWidth = width
        path.stroke()
    }

    func line(_ x1: CGFloat, _ y1: CGFloat, _ x2: CGFloat, _ y2: CGFloat, _ c: NSColor, _ width: CGFloat) {
        let path = NSBezierPath()
        path.move(to: NSPoint(x: x1, y: bounds.height - y1))
        path.line(to: NSPoint(x: x2, y: bounds.height - y2))
        c.setStroke()
        path.lineWidth = width
        path.lineCapStyle = .round
        path.stroke()
    }

    override func draw(_ dirtyRect: NSRect) {
        let state = status["state"] ?? "idle"
        let (bodyFill, accent) = palette(state)

        oval(53, 178, 74, 18, color("#111827").withAlphaComponent(0.22), NSColor.clear, 0)

        oval(36, 28, 54, 60, color("#88aaff"))
        oval(74, 22, 62, 66, color("#9bb8ff"))
        oval(24, 55, 50, 55, color("#6f90ed"))
        oval(120, 58, 42, 50, color("#7698f2"))
        roundRect(68, 40, 46, 18, 5, .white)
        text("+", 68, 38, 46, 22, 14, accent, true)

        roundRect(50, 72, 82, 47, 13, color("#172554"), color("#111827"), 3)
        let eye = state == "intervening" ? NSColor.white : color("#93c5fd")
        line(70, 93, 80, 98, eye, 3)
        line(80, 98, 86, 93, eye, 3)
        line(100, 93, 110, 98, eye, 3)
        line(110, 98, 116, 93, eye, 3)

        roundRect(64, 128, 54, 50, 10, bodyFill, color("#111827"), 3)
        line(91, 130, 91, 176, color("#cbd5e1"), 2)
        line(76, 140, 89, 153, color("#cbd5e1"), 2)
        line(106, 140, 93, 153, color("#cbd5e1"), 2)
        oval(79, 145, 24, 24, NSColor.clear, accent, 3)
        line(91, 169, 91, 177, accent, 3)
        line(63, 140, 42, 166, color("#5b7ee5"), 11)
        line(119, 140, 140, 166, color("#5b7ee5"), 11)
        line(78, 178, 74, 199, color("#5b7ee5"), 12)
        line(104, 178, 108, 199, color("#5b7ee5"), 12)

        if state == "intervening" {
            oval(136, 26, 24, 24, accent, .white, 2)
            text("!", 136, 28, 24, 20, 15, .white, true)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
let petWidth: CGFloat = 180
let petHeight: CGFloat = 210
let startFrame = NSRect(
    x: screenFrame.maxX - petWidth - 80,
    y: screenFrame.maxY - petHeight - 80,
    width: petWidth,
    height: petHeight
)

let window = NSWindow(
    contentRect: startFrame,
    styleMask: [.borderless],
    backing: .buffered,
    defer: false
)
window.title = "Agent Doctor Pet"
window.isReleasedWhenClosed = false
window.isOpaque = false
window.backgroundColor = .clear
window.hasShadow = false
window.isMovableByWindowBackground = true
window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
if topmost {
    window.level = .floating
}

let view = PetView(frame: NSRect(x: 0, y: 0, width: 180, height: 210))
view.wantsLayer = true
view.layer?.backgroundColor = NSColor.clear.cgColor
window.contentView = view
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)

Timer.scheduledTimer(withTimeInterval: max(0.2, pollSeconds), repeats: true) { _ in
    view.status = loadStatus()
}

app.run()
'''
