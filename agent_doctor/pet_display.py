"""Desktop display for Doctor Pet.

This module is intentionally optional UI glue. It lazy-imports ``tkinter`` so
the scan/apply/autopilot production path remains dependency-free and headless
safe. The display reads ``pet-status.json`` and renders a small always-on-top
doctor pet window that refreshes as autopilot updates the file.
"""

from __future__ import annotations

import json
import math
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir
from typing import Any

_WINDOW_WIDTH = 190
_WINDOW_HEIGHT = 210
_ASSET_NAME = "doctor_pet.png"


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


def pet_asset_path() -> Path | None:
    path = Path(__file__).with_name("assets") / _ASSET_NAME
    if path.exists():
        return path
    return None


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
                asset_path=pet_asset_path(),
            )
            return
        raise RuntimeError(
            "Doctor Pet desktop display requires tkinter, or Swift/AppKit on macOS. "
            "Use `agent-doctor pet --out <dir>` to render files without a desktop window."
        ) from exc

    status_path = (status_file or default_status_file()).expanduser()
    asset_path = pet_asset_path()
    poll_interval = max(0.2, poll_seconds)

    root = tk.Tk()
    root.title("Agent Doctor Pet")
    root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}+120+120")
    root.resizable(False, False)
    root.overrideredirect(True)
    root.configure(bg="#ff00ff")
    if topmost:
        root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", "#ff00ff")
    except Exception:
        pass

    canvas = tk.Canvas(
        root,
        width=_WINDOW_WIDTH,
        height=_WINDOW_HEIGHT,
        bg="#ff00ff",
        highlightthickness=0,
    )
    canvas.pack(fill="both", expand=True)
    pet_image = None
    if asset_path is not None:
        try:
            raw_image = tk.PhotoImage(file=str(asset_path))
            pet_image = raw_image.subsample(3, 3)
        except Exception:
            pet_image = None
    drag = {"x": 0, "y": 0}

    def start_drag(event: Any) -> None:
        drag["x"] = event.x
        drag["y"] = event.y

    def move_drag(event: Any) -> None:
        root.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

    canvas.bind("<ButtonPress-1>", start_drag)
    canvas.bind("<B1-Motion>", move_drag)

    status_cache: dict[str, Any] = {
        "read_at": 0.0,
        "snapshot": snapshot_from_payload(read_status_payload(status_path)),
    }

    def draw() -> None:
        now = time.monotonic()
        if now - float(status_cache["read_at"]) >= poll_interval:
            status_cache["snapshot"] = snapshot_from_payload(read_status_payload(status_path))
            status_cache["read_at"] = now
        snapshot = status_cache["snapshot"]
        canvas.delete("all")
        _draw_pet(canvas, snapshot, phase=now, pet_image=pet_image)
        canvas.after(66, draw)

    draw()
    root.mainloop()


def _draw_pet(
    canvas: Any,
    snapshot: DisplaySnapshot,
    *,
    phase: float = 0.0,
    pet_image: Any | None = None,
) -> None:
    canvas.create_rectangle(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT, fill="#ff00ff", outline="")
    if pet_image is not None:
        _draw_sprite_pet(canvas, snapshot, phase=phase, pet_image=pet_image)
        return
    _draw_vector_pet(canvas, snapshot, phase=phase)


def _draw_sprite_pet(
    canvas: Any,
    snapshot: DisplaySnapshot,
    *,
    phase: float,
    pet_image: Any,
) -> None:
    bob = _bob_for_state(snapshot.state, phase)
    center_x = _WINDOW_WIDTH / 2
    center_y = 102 - bob

    _draw_tk_effects(canvas, snapshot, phase)
    shadow_width = 66 + (5 * math.sin(phase * 2.0))
    canvas.create_oval(
        center_x - shadow_width / 2,
        182,
        center_x + shadow_width / 2,
        197,
        fill="#111827",
        outline="",
        stipple="gray50",
    )
    canvas.create_image(center_x, center_y, image=pet_image)
    if snapshot.state == "watching":
        scan_y = 80 + (18 * ((math.sin(phase * 3.2) + 1) / 2))
        canvas.create_line(56, scan_y, 134, scan_y, fill="#9ff6ff", width=3)
    elif snapshot.state == "concerned":
        ring = 20 + (10 * ((math.sin(phase * 4.0) + 1) / 2))
        canvas.create_oval(
            center_x - ring,
            130 - ring,
            center_x + ring,
            130 + ring,
            outline=snapshot.accent,
            width=2,
        )
    elif snapshot.state == "intervening":
        canvas.create_oval(142, 26, 171, 55, fill=snapshot.accent, outline="#ffffff", width=2)
        canvas.create_text(156, 39, text="!", fill="#ffffff", font=("Helvetica", 17, "bold"))
        pulse = 3 + (8 * ((math.sin(phase * 5.5) + 1) / 2))
        canvas.create_oval(
            142 - pulse,
            26 - pulse,
            171 + pulse,
            55 + pulse,
            outline=snapshot.accent,
            width=2,
        )


def _draw_tk_effects(canvas: Any, snapshot: DisplaySnapshot, phase: float) -> None:
    pulse = (math.sin(phase * 2.0) + 1) / 2
    if snapshot.state == "idle":
        canvas.create_oval(48, 36, 142, 134, fill="#dbeafe", outline="")
    elif snapshot.state == "watching":
        canvas.create_oval(35, 28, 155, 148, outline="#60a5fa", width=2)
        x = 48 + (94 * pulse)
        canvas.create_oval(x - 4, 27, x + 4, 35, fill="#9ff6ff", outline="")
    elif snapshot.state == "concerned":
        size = 104 + (18 * pulse)
        canvas.create_oval(
            95 - size / 2,
            88 - size / 2,
            95 + size / 2,
            88 + size / 2,
            outline="#f59e0b",
            width=3,
        )
    elif snapshot.state == "intervening":
        size = 118 + (20 * pulse)
        canvas.create_oval(
            95 - size / 2,
            86 - size / 2,
            95 + size / 2,
            86 + size / 2,
            outline=snapshot.accent,
            width=4,
        )


def _bob_for_state(state: str, phase: float) -> float:
    if state == "intervening":
        return 4.0 + (2.0 * math.sin(phase * 8.0))
    if state == "concerned":
        return 2.5 + (1.5 * math.sin(phase * 4.0))
    if state == "watching":
        return 3.0 + (2.0 * math.sin(phase * 2.8))
    return 2.0 + (1.2 * math.sin(phase * 1.8))


def _draw_vector_pet(canvas: Any, snapshot: DisplaySnapshot, *, phase: float) -> None:
    bob = _bob_for_state(snapshot.state, phase)

    # Soft shadow.
    canvas.create_oval(54, 178, 128, 196, fill="#1f2937", outline="", stipple="gray50")

    # Cloud hair / doctor cap silhouette.
    canvas.create_oval(36, 28 - bob, 90, 88 - bob, fill="#88aaff", outline="#111827", width=2)
    canvas.create_oval(74, 22 - bob, 136, 88 - bob, fill="#9bb8ff", outline="#111827", width=2)
    canvas.create_oval(24, 55 - bob, 74, 110 - bob, fill="#6f90ed", outline="#111827", width=2)
    canvas.create_oval(120, 58 - bob, 162, 108 - bob, fill="#7698f2", outline="#111827", width=2)
    canvas.create_rectangle(68, 40 - bob, 114, 58 - bob, fill="#ffffff", outline="#111827", width=2)
    canvas.create_text(91, 49 - bob, text="+", fill=snapshot.accent, font=("Helvetica", 14, "bold"))

    # Face screen.
    canvas.create_rectangle(50, 72 - bob, 132, 119 - bob, fill="#172554", outline="#111827", width=3)
    eye_color = "#93c5fd" if snapshot.state != "intervening" else "#ffffff"
    canvas.create_arc(70, 90 - bob, 86, 105 - bob, start=200, extent=140, style="arc", outline=eye_color, width=3)
    canvas.create_arc(100, 90 - bob, 116, 105 - bob, start=200, extent=140, style="arc", outline=eye_color, width=3)

    # Body / coat.
    canvas.create_rectangle(64, 128 - bob, 118, 178 - bob, fill=snapshot.fill, outline="#111827", width=3)
    canvas.create_line(91, 130 - bob, 91, 177 - bob, fill="#cbd5e1", width=2)
    canvas.create_line(76, 140 - bob, 89, 153 - bob, fill="#cbd5e1", width=2)
    canvas.create_line(106, 140 - bob, 93, 153 - bob, fill="#cbd5e1", width=2)
    canvas.create_oval(79, 145 - bob, 103, 169 - bob, outline=snapshot.accent, width=3)
    canvas.create_line(91, 169 - bob, 91, 177 - bob, fill=snapshot.accent, width=3)

    # Arms / legs.
    canvas.create_line(63, 140 - bob, 42, 166 - bob, fill="#5b7ee5", width=11)
    canvas.create_line(119, 140 - bob, 140, 166 - bob, fill="#5b7ee5", width=11)
    canvas.create_line(78, 178 - bob, 74, 199 - bob, fill="#5b7ee5", width=12)
    canvas.create_line(104, 178 - bob, 108, 199 - bob, fill="#5b7ee5", width=12)

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
    asset_path: Path | None = None,
) -> None:
    source_path = Path(gettempdir()) / "agent_doctor_pet_display.swift"
    source_path.write_text(_appkit_source(), encoding="utf-8")
    command = [
        "swift",
        str(source_path),
        str(status_file.expanduser()),
        str(max(0.2, poll_seconds)),
        "1" if topmost else "0",
        str(asset_path.expanduser()) if asset_path is not None else "",
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
let assetPath = CommandLine.arguments.count > 4 ? CommandLine.arguments[4] : ""
let windowWidth: CGFloat = 190
let windowHeight: CGFloat = 210

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

func palette(_ state: String) -> (NSColor, NSColor, NSColor) {
    if state == "intervening" {
        return (color("#fee2e2"), color("#b42318"), color("#f97316"))
    }
    if state == "concerned" {
        return (color("#fef3c7"), color("#b54708"), color("#f59e0b"))
    }
    if state == "watching" {
        return (color("#dbeafe"), color("#175cd3"), color("#38bdf8"))
    }
    return (color("#eff6ff"), color("#3556c7"), color("#93c5fd"))
}

func pulse(_ t: Double, _ speed: Double) -> CGFloat {
    return CGFloat((sin(t * speed) + 1.0) / 2.0)
}

func bob(_ state: String, _ t: Double) -> CGFloat {
    if state == "intervening" {
        return 4.0 + (2.0 * CGFloat(sin(t * 8.0)))
    }
    if state == "concerned" {
        return 2.5 + (1.5 * CGFloat(sin(t * 4.0)))
    }
    if state == "watching" {
        return 3.0 + (2.0 * CGFloat(sin(t * 2.8)))
    }
    return 2.0 + (1.2 * CGFloat(sin(t * 1.8)))
}

class PetView: NSView {
    var status: [String: String] = loadStatus() {
        didSet { needsDisplay = true }
    }
    var dragOffset: NSPoint = .zero
    var startedAt = Date()
    var lastStatusReload = Date(timeIntervalSince1970: 0)
    let petImage: NSImage? = assetPath.isEmpty ? nil : NSImage(contentsOfFile: assetPath)

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
        if width > 0 {
            stroke.setStroke()
            path.lineWidth = width
            path.stroke()
        }
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

    func pathLine(_ points: [NSPoint], _ colorValue: NSColor, _ width: CGFloat) {
        guard let first = points.first else { return }
        let path = NSBezierPath()
        path.move(to: first)
        for point in points.dropFirst() {
            path.line(to: point)
        }
        colorValue.setStroke()
        path.lineWidth = width
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        path.stroke()
    }

    func drawEffects(_ state: String, _ t: Double, _ accent: NSColor, _ glow: NSColor) {
        let p = pulse(t, 2.0)
        if state == "idle" {
            oval(39, 35, 112, 112, glow.withAlphaComponent(0.12 + (0.07 * p)), NSColor.clear, 0)
            oval(62, 50, 66, 66, glow.withAlphaComponent(0.08), NSColor.clear, 0)
        } else if state == "watching" {
            oval(35, 27, 120, 120, accent.withAlphaComponent(0.08), glow.withAlphaComponent(0.55), 2)
            let x = 47 + (96 * p)
            oval(x - 4, 25, 8, 8, glow.withAlphaComponent(0.95), NSColor.clear, 0)
        } else if state == "concerned" {
            let size = 104 + (18 * p)
            oval(95 - size / 2, 87 - size / 2, size, size, color("#f59e0b").withAlphaComponent(0.08), accent.withAlphaComponent(0.62), 3)
            let y = bounds.height - 174
            pathLine([
                NSPoint(x: 54, y: y),
                NSPoint(x: 70, y: y),
                NSPoint(x: 77, y: y + 7),
                NSPoint(x: 86, y: y - 8),
                NSPoint(x: 96, y: y + 10),
                NSPoint(x: 107, y: y),
                NSPoint(x: 132, y: y)
            ], accent.withAlphaComponent(0.75), 3)
        } else if state == "intervening" {
            let size = 116 + (22 * p)
            oval(95 - size / 2, 86 - size / 2, size, size, color("#ef4444").withAlphaComponent(0.08), accent.withAlphaComponent(0.75), 4)
            oval(49, 37, 92, 92, color("#fee2e2").withAlphaComponent(0.13), NSColor.clear, 0)
        }
    }

    func drawSprite(_ state: String, _ t: Double) {
        guard let image = petImage else {
            drawFallbackVector(state, t)
            return
        }
        let lift = bob(state, t)
        let scale = 1.0 + (0.018 * CGFloat(sin(t * 2.2)))
        let rect = r(15, 20 - lift, 160, 160)
        let center = NSPoint(x: rect.midX, y: rect.midY)
        NSGraphicsContext.saveGraphicsState()
        let transform = NSAffineTransform()
        transform.translateX(by: center.x, yBy: center.y)
        transform.scale(by: scale)
        transform.translateX(by: -center.x, yBy: -center.y)
        transform.concat()
        image.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
        NSGraphicsContext.restoreGraphicsState()
    }

    func drawFallbackVector(_ state: String, _ t: Double) {
        let lift = bob(state, t)
        let (_, accent, _) = palette(state)
        oval(36, 28 - lift, 54, 60, color("#88aaff"))
        oval(74, 22 - lift, 62, 66, color("#9bb8ff"))
        oval(24, 55 - lift, 50, 55, color("#6f90ed"))
        oval(120, 58 - lift, 42, 50, color("#7698f2"))
        roundRect(68, 40 - lift, 46, 18, 5, .white)
        text("+", 68, 38 - lift, 46, 22, 14, accent, true)
        roundRect(50, 72 - lift, 82, 47, 13, color("#172554"), color("#111827"), 3)
        let eye = state == "intervening" ? NSColor.white : color("#93c5fd")
        line(70, 93 - lift, 80, 98 - lift, eye, 3)
        line(80, 98 - lift, 86, 93 - lift, eye, 3)
        line(100, 93 - lift, 110, 98 - lift, eye, 3)
        line(110, 98 - lift, 116, 93 - lift, eye, 3)
        roundRect(64, 128 - lift, 54, 50, 10, color("#eff6ff"), color("#111827"), 3)
        line(63, 140 - lift, 42, 166 - lift, color("#5b7ee5"), 11)
        line(119, 140 - lift, 140, 166 - lift, color("#5b7ee5"), 11)
        line(78, 178 - lift, 74, 199 - lift, color("#5b7ee5"), 12)
        line(104, 178 - lift, 108, 199 - lift, color("#5b7ee5"), 12)
    }

    func drawOverlays(_ state: String, _ t: Double, _ accent: NSColor, _ glow: NSColor) {
        if state == "watching" {
            let scanY = 83 + (18 * pulse(t, 3.2))
            line(56, scanY, 134, scanY, glow.withAlphaComponent(0.9), 3)
            line(61, scanY + 5, 129, scanY + 5, glow.withAlphaComponent(0.28), 6)
        } else if state == "concerned" {
            let ring = 20 + (10 * pulse(t, 4.0))
            oval(95 - ring, 128 - ring, ring * 2, ring * 2, NSColor.clear, accent.withAlphaComponent(0.72), 2)
        } else if state == "intervening" {
            let p = pulse(t, 5.5)
            roundRect(142, 25, 30, 30, 15, accent, .white, 2)
            text("!", 142, 28, 30, 22, 17, .white, true)
            oval(142 - (8 * p), 25 - (8 * p), 30 + (16 * p), 30 + (16 * p), NSColor.clear, accent.withAlphaComponent(0.7), 2)
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        let state = status["state"] ?? "idle"
        let (_, accent, glow) = palette(state)
        let t = Date().timeIntervalSince(startedAt)
        let shadowPulse = 1.0 + (0.08 * pulse(t, 2.0))

        drawEffects(state, t, accent, glow)
        oval(57 - (3 * shadowPulse), 180, 76 + (6 * shadowPulse), 16, color("#111827").withAlphaComponent(0.22), NSColor.clear, 0)
        drawSprite(state, t)
        drawOverlays(state, t, accent, glow)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
let startFrame = NSRect(
    x: screenFrame.maxX - windowWidth - 80,
    y: screenFrame.maxY - windowHeight - 80,
    width: windowWidth,
    height: windowHeight
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

let view = PetView(frame: NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight))
view.wantsLayer = true
view.layer?.backgroundColor = NSColor.clear.cgColor
window.contentView = view
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)

Timer.scheduledTimer(withTimeInterval: 1.0 / 15.0, repeats: true) { _ in
    let now = Date()
    if now.timeIntervalSince(view.lastStatusReload) >= max(0.2, pollSeconds) {
        view.status = loadStatus()
        view.lastStatusReload = now
    } else {
        view.needsDisplay = true
    }
}

app.run()
'''
