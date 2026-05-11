import Cocoa
import UniformTypeIdentifiers

let statusPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let pollSeconds = CommandLine.arguments.count > 2 ? (Double(CommandLine.arguments[2]) ?? 1.0) : 1.0
let topmost = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] == "1" : true
let assetPath = CommandLine.arguments.count > 4 ? CommandLine.arguments[4] : ""
let pythonExecutable = CommandLine.arguments.count > 5 ? CommandLine.arguments[5] : "/usr/bin/python3"
let userSpritePath = CommandLine.arguments.count > 6 ? CommandLine.arguments[6] : ""
let packagedSpritePath = CommandLine.arguments.count > 7 ? CommandLine.arguments[7] : ""
let compactWindowWidth: CGFloat = 260
let compactWindowHeight: CGFloat = 310
let expandedWindowWidth: CGFloat = 360
let expandedWindowHeight: CGFloat = 560
let idleExpandedWindowHeight: CGFloat = 430
let idleNoticeExpandedWindowHeight: CGFloat = 500

enum SpriteWatcher {
    static func modificationDate(at path: String) -> Date? {
        if path.isEmpty {
            return nil
        }
        let attrs = try? FileManager.default.attributesOfItem(atPath: path)
        return attrs?[.modificationDate] as? Date
    }
}

func stringValue(_ dict: [String: Any], _ key: String, _ fallback: String) -> String {
    if let value = dict[key] as? String {
        return value
    }
    if let value = dict[key] as? NSNumber {
        return value.stringValue
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
            "platform": "generic",
            "phase": "healthy",
            "headline": "Agent Doctor is waiting for status.",
            "message": "Status file not found yet.",
            "emotion_message": "",
            "diagnosis": "No active incident was detected.",
            "recommendation": "Keep Agent Doctor running while the session continues.",
            "recovery_prompt": "",
            "expires_after_seconds": "120",
            "session_id": "",
            "card_path": "",
            "latest_event_id": "",
            "latest_trigger": "",
            "dismiss_state_path": "",
            "evidence_count": "0",
            "option_count": "0"
        ]
    }
    guard
        let obj = try? JSONSerialization.jsonObject(with: data),
        let dict = obj as? [String: Any]
    else {
        return [
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "platform": "generic",
            "phase": "healthy",
            "headline": "Agent Doctor is waiting for a valid status.",
            "message": "Expected a JSON object.",
            "emotion_message": "",
            "diagnosis": "Agent Doctor could not use the latest status update.",
            "recommendation": "Keep Agent Doctor running. The next valid status write will refresh this panel.",
            "recovery_prompt": "",
            "expires_after_seconds": "120",
            "session_id": "",
            "card_path": "",
            "latest_event_id": "",
            "latest_trigger": "",
            "dismiss_state_path": "",
            "evidence_count": "0",
            "option_count": "0"
        ]
    }
    let options = dict["options"] as? [[String: Any]] ?? []
    let limitedOptions = Array(options.prefix(6))
    let evidence = dict["evidence"] as? [[String: Any]] ?? []
    let limitedEvidence = Array(evidence.prefix(3))
    var result = [
        "state": stringValue(dict, "state", "idle"),
        "action": stringValue(dict, "action", "silent"),
        "severity": stringValue(dict, "severity", "low"),
        "platform": stringValue(dict, "platform", "generic"),
        "phase": stringValue(dict, "phase", "healthy"),
        "headline": stringValue(dict, "headline", "Agent Doctor is idle."),
        "message": stringValue(dict, "message", ""),
        "emotion_message": stringValue(dict, "emotion_message", ""),
        "diagnosis": stringValue(dict, "diagnosis", ""),
        "recommendation": stringValue(dict, "recommendation", ""),
        "recovery_prompt": stringValue(dict, "recovery_prompt", ""),
        "expires_after_seconds": stringValue(dict, "expires_after_seconds", "120"),
        "session_id": stringValue(dict, "session_id", ""),
        "card_path": stringValue(dict, "card_path", ""),
        "latest_event_id": stringValue(dict, "latest_event_id", ""),
        "latest_trigger": stringValue(dict, "latest_trigger", ""),
        "dismiss_state_path": stringValue(dict, "dismiss_state_path", ""),
        "evidence_count": String(limitedEvidence.count),
        "option_count": String(limitedOptions.count)
    ]
    for (index, item) in limitedEvidence.enumerated() {
        result["evidence_\(index)_file"] = stringValue(item, "file", "")
        result["evidence_\(index)_line"] = stringValue(item, "line", "")
        result["evidence_\(index)_role"] = stringValue(item, "role", "")
        result["evidence_\(index)_quote"] = stringValue(item, "quote", "")
    }
    for (index, option) in limitedOptions.enumerated() {
        result["option_\(index)_id"] = stringValue(option, "id", "")
        result["option_\(index)_label"] = stringValue(option, "label", "")
        result["option_\(index)_description"] = stringValue(option, "description", "")
        result["option_\(index)_command"] = stringValue(option, "command", "")
    }
    return result
}

func currentStatusSnapshotData(_ status: [String: String]) -> Data? {
    var payload: [String: Any] = [
        "state": status["state"] ?? "idle",
        "action": status["action"] ?? "silent",
        "severity": status["severity"] ?? "low",
        "platform": status["platform"] ?? "generic",
        "phase": status["phase"] ?? "healthy",
        "headline": status["headline"] ?? "",
        "message": status["message"] ?? "",
        "emotion_message": status["emotion_message"] ?? "",
        "diagnosis": status["diagnosis"] ?? "",
        "recommendation": status["recommendation"] ?? "",
        "recovery_prompt": status["recovery_prompt"] ?? "",
        "expires_after_seconds": status["expires_after_seconds"] ?? "120",
        "session_id": status["session_id"] ?? "",
        "card_path": status["card_path"] ?? "",
        "latest_event_id": status["latest_event_id"] ?? "",
        "latest_trigger": status["latest_trigger"] ?? "",
        "dismiss_state_path": status["dismiss_state_path"] ?? ""
    ]
    var evidence: [[String: Any]] = []
    let evidenceCount = Int(status["evidence_count"] ?? "0") ?? 0
    for index in 0..<evidenceCount {
        evidence.append([
            "file": status["evidence_\(index)_file"] ?? "",
            "line": status["evidence_\(index)_line"] ?? "",
            "role": status["evidence_\(index)_role"] ?? "",
            "quote": status["evidence_\(index)_quote"] ?? ""
        ])
    }
    payload["evidence"] = evidence
    var options: [[String: Any]] = []
    let optionCount = Int(status["option_count"] ?? "0") ?? 0
    for index in 0..<optionCount {
        options.append([
            "id": status["option_\(index)_id"] ?? "",
            "label": status["option_\(index)_label"] ?? "",
            "description": status["option_\(index)_description"] ?? "",
            "command": status["option_\(index)_command"] ?? ""
        ])
    }
    payload["options"] = options
    return try? JSONSerialization.data(withJSONObject: payload)
}

func writeCurrentStatusSnapshot(_ status: [String: String]) throws -> String {
    guard let data = currentStatusSnapshotData(status) else {
        throw NSError(
            domain: "AgentDoctor",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Could not serialize current Agent Doctor status."]
        )
    }
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("agent-doctor-send-\(UUID().uuidString).json")
    try data.write(to: url, options: .atomic)
    return url.path
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

final class ProcessOutputCollector {
    private let pipe: Pipe
    private var data = Data()
    private let lock = NSLock()

    init(_ pipe: Pipe) {
        self.pipe = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            self?.append(chunk)
        }
    }

    private func append(_ chunk: Data) {
        lock.lock()
        data.append(chunk)
        lock.unlock()
    }

    func finish() -> String {
        pipe.fileHandleForReading.readabilityHandler = nil
        let tail = pipe.fileHandleForReading.availableData
        if !tail.isEmpty {
            append(tail)
        }
        lock.lock()
        let snapshot = data
        lock.unlock()
        return String(data: snapshot, encoding: .utf8) ?? ""
    }
}

class PetView: NSView {
    var status: [String: String] = loadStatus() {
        didSet {
            observeCurrentEvent()
            needsDisplay = true
        }
    }
    var dragOffset: NSPoint = .zero
    var isDragging = false
    var bubbleOpen = false
    var dismissedEventId = ""
    var activeEventId = ""
    var eventFirstSeenAt = Date()
    var startedAt = Date()
    var lastStatusReload = Date(timeIntervalSince1970: 0)
    var buttonFrames: [(String, NSRect)] = []
    var noticeText = ""
    var deliveryResultText = ""
    var deliveryResultSucceeded = false
    var deliveryResultUntil = Date(timeIntervalSince1970: 0)
    var deliveryEventId = ""
    var runningActionId = ""
    var activeProcesses: [Process] = []
    var statusReloadInFlight = false
    // Initial sprite load happens via reloadSpriteIfChanged() right after the
    // view is created, so the very first paint already uses currentSpritePath()
    // (prefer user override → packaged → legacy launch-time assetPath) instead
    // of trusting whichever single path Python resolved at launch.
    var petImage: NSImage? = nil
    var lastSpritePath: String = ""
    var lastSpriteMTime: Date? = nil

    /// Pick the sprite path to use right now: prefer the user override if it
    /// exists on disk, otherwise fall back to the packaged sprite, otherwise
    /// the legacy launch-time ``assetPath``. This lets the AppKit pet pick up
    /// a freshly-installed user sprite without a restart, even when the
    /// window launched before ``~/.agent-doctor/pet/sprite.png`` existed —
    /// and lets it revert to the packaged default when the user sprite is
    /// deleted at runtime.
    func currentSpritePath() -> String {
        if !userSpritePath.isEmpty,
           FileManager.default.fileExists(atPath: userSpritePath) {
            return userSpritePath
        }
        if !packagedSpritePath.isEmpty {
            return packagedSpritePath
        }
        return assetPath
    }

    func reloadSpriteIfChanged() {
        let chosen = currentSpritePath()
        if chosen.isEmpty {
            return
        }
        let current = SpriteWatcher.modificationDate(at: chosen)
        // A path change must force a reload even when mtime would otherwise
        // tie (e.g. switching back to the packaged default after the user
        // sprite is deleted), so we cache (path, mtime) as a tuple.
        if chosen == lastSpritePath, lastSpriteMTime == current {
            return
        }
        lastSpritePath = chosen
        lastSpriteMTime = current
        if let refreshed = NSImage(contentsOfFile: chosen) {
            petImage = refreshed
            needsDisplay = true
        }
    }

    // ------------------------------------------------------------
    //  Generation activity indicator
    //
    //  Gemini image-gen takes several seconds. Without feedback the user
    //  clicks "Generate sprite from prompt..." and sees nothing happen
    //  until the sprite swaps. Show a dark translucent pill in the centre
    //  of the pet with a spinning indeterminate progress indicator while
    //  the subprocess is running. The pill is sized small (44×44) so it
    //  obscures the pet's centre only, not the whole sprite, and uses
    //  rounded corners so it reads as an iOS-style HUD rather than a
    //  rectangle. Built lazily on first use; reused across invocations.
    // ------------------------------------------------------------

    var generationHud: NSView? = nil
    var generationSpinner: NSProgressIndicator? = nil

    private func ensureGenerationHud() -> (NSView, NSProgressIndicator) {
        if let hud = generationHud, let spin = generationSpinner {
            // Re-centre in case the view was resized between invocations.
            hud.frame = generationHudFrame()
            return (hud, spin)
        }
        let hud = NSView(frame: generationHudFrame())
        hud.wantsLayer = true
        hud.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.55).cgColor
        hud.layer?.cornerRadius = hud.frame.height / 2.0
        hud.layer?.masksToBounds = true
        hud.autoresizingMask = [.minXMargin, .maxXMargin, .minYMargin, .maxYMargin]
        hud.isHidden = true
        addSubview(hud)

        let spinSize: CGFloat = 22
        let spin = NSProgressIndicator(frame: NSRect(
            x: (hud.frame.width - spinSize) / 2.0,
            y: (hud.frame.height - spinSize) / 2.0,
            width: spinSize,
            height: spinSize
        ))
        spin.style = .spinning
        spin.isIndeterminate = true
        spin.controlSize = .regular
        spin.isDisplayedWhenStopped = false
        // Force the light-on-dark spinner variant so the pinwheel reads
        // against the dark pill regardless of the user's system appearance.
        spin.appearance = NSAppearance(named: .darkAqua)
        hud.addSubview(spin)

        generationHud = hud
        generationSpinner = spin
        return (hud, spin)
    }

    private func generationHudFrame() -> NSRect {
        let side: CGFloat = 44
        return NSRect(
            x: bounds.midX - side / 2.0,
            y: bounds.midY - side / 2.0,
            width: side,
            height: side
        )
    }

    func startGenerationIndicator() {
        let (hud, spin) = ensureGenerationHud()
        hud.isHidden = false
        spin.startAnimation(nil)
    }

    func stopGenerationIndicator() {
        generationSpinner?.stopAnimation(nil)
        generationHud?.isHidden = true
    }

    override var isOpaque: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }

    override func mouseDown(with event: NSEvent) {
        dragOffset = event.locationInWindow
        isDragging = false
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window = self.window else { return }
        isDragging = true
        let mouse = NSEvent.mouseLocation
        window.setFrameOrigin(NSPoint(x: mouse.x - dragOffset.x, y: mouse.y - dragOffset.y))
    }

    override func mouseUp(with event: NSEvent) {
        if isDragging {
            return
        }
        let point = convert(event.locationInWindow, from: nil)
        if performButton(at: point) {
            return
        }
        bubbleOpen = !bubbleOpen
        noticeText = ""
        needsDisplay = true
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu(title: "Agent Doctor")
        menu.addItem(
            withTitle: "Change sprite...",
            action: #selector(changeSprite(_:)),
            keyEquivalent: ""
        ).target = self
        menu.addItem(
            withTitle: "Reset to default",
            action: #selector(resetSprite(_:)),
            keyEquivalent: ""
        ).target = self
        menu.addItem(NSMenuItem.separator())
        menu.addItem(
            withTitle: "Generate sprite from prompt...",
            action: #selector(generateSpriteFromPrompt(_:)),
            keyEquivalent: ""
        ).target = self
        menu.addItem(
            withTitle: "Configure Gemini...",
            action: #selector(configureGemini(_:)),
            keyEquivalent: ""
        ).target = self
        menu.addItem(NSMenuItem.separator())
        menu.addItem(
            withTitle: "Quit",
            action: #selector(closePetFromMenu(_:)),
            keyEquivalent: ""
        ).target = self
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    /// Show an NSAlert with an editable text field for the Gemini prompt.
    /// On confirm, shells `agent-doctor pet-generate-sprite --prompt <text>`
    /// and refreshes the displayed sprite via the same hot-reload path
    /// used by `pet-set-sprite` (PR #18).
    @objc func generateSpriteFromPrompt(_ sender: Any?) {
        let alert = NSAlert()
        alert.messageText = "Generate sprite from prompt"
        alert.informativeText = "Describe the pet you want. Example: \"a cute orange tabby cat astronaut, sticker style, white background\"."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Generate")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.placeholderString = "Describe the desktop pet"
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        let response = alert.runModal()
        if response != .alertFirstButtonReturn {
            return
        }
        let prompt = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if prompt.isEmpty {
            let warn = NSAlert()
            warn.messageText = "Prompt is empty"
            warn.informativeText = "Type a description, then choose Generate."
            warn.alertStyle = .warning
            warn.addButton(withTitle: "OK")
            warn.runModal()
            return
        }
        runGenerateSpriteProcess(prompt: prompt)
    }

    func runGenerateSpriteProcess(prompt: String) {
        // Start the HUD on the main thread BEFORE dispatching so the user
        // sees the spinner the same frame they dismissed the prompt alert.
        // Stopping always happens before the success/error branch on the
        // way back so the HUD never lingers past the subprocess.
        startGenerationIndicator()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: pythonExecutable)
            process.arguments = [
                "-m",
                "agent_doctor.cli",
                "pet-generate-sprite",
                "--prompt",
                prompt
            ]
            let errorPipe = Pipe()
            let errorCollector = ProcessOutputCollector(errorPipe)
            process.standardError = errorPipe
            do {
                try process.run()
                process.waitUntilExit()
            } catch {
                DispatchQueue.main.async {
                    self?.stopGenerationIndicator()
                    self?.showSpriteError(error.localizedDescription)
                }
                return
            }
            let stderr = errorCollector.finish()
            DispatchQueue.main.async {
                self?.stopGenerationIndicator()
                if process.terminationStatus != 0 {
                    self?.showSpriteError(stderr)
                } else {
                    // Same refresh path as pet-set-sprite (PR #18): the user
                    // sprite mtime changed, so the next status-poll tick
                    // (or this explicit call) reloads NSImage in place.
                    self?.reloadSpriteIfChanged()
                }
            }
        }
    }

    /// Show a NSSecureTextField NSAlert for the API key, then shell
    /// `agent-doctor settings set-gemini-key --from-env <ENV>` with the
    /// key passed via the child process's environment so it never lands
    /// in argv (and therefore never in shell history or `ps` output).
    @objc func configureGemini(_ sender: Any?) {
        let alert = NSAlert()
        alert.messageText = "Configure Gemini"
        alert.informativeText = "Paste your Gemini API key. It is stored in macOS Keychain (preferred) or ~/.agent-doctor/config.toml (mode 0600)."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.placeholderString = "Gemini API key"
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        let response = alert.runModal()
        if response != .alertFirstButtonReturn {
            return
        }
        let key = field.stringValue
        if key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let warn = NSAlert()
            warn.messageText = "No key provided"
            warn.informativeText = "Paste the key, then choose Save."
            warn.alertStyle = .warning
            warn.addButton(withTitle: "OK")
            warn.runModal()
            return
        }
        runSetGeminiKeyProcess(key: key)
    }

    func runSetGeminiKeyProcess(key: String) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: pythonExecutable)
            // Use a fixed env-var name and pipe the key through environment,
            // not argv. CLI reads it via `--from-env AGENT_DOCTOR_GEMINI_API_KEY`.
            let envName = "AGENT_DOCTOR_GEMINI_API_KEY"
            process.arguments = [
                "-m",
                "agent_doctor.cli",
                "settings",
                "set-gemini-key",
                "--from-env",
                envName
            ]
            var environment = ProcessInfo.processInfo.environment
            environment[envName] = key
            process.environment = environment
            let errorPipe = Pipe()
            let errorCollector = ProcessOutputCollector(errorPipe)
            process.standardError = errorPipe
            let outputPipe = Pipe()
            let outputCollector = ProcessOutputCollector(outputPipe)
            process.standardOutput = outputPipe
            do {
                try process.run()
                process.waitUntilExit()
            } catch {
                DispatchQueue.main.async {
                    self?.showGeminiConfigError(error.localizedDescription)
                }
                return
            }
            let stderr = errorCollector.finish()
            _ = outputCollector.finish()
            DispatchQueue.main.async {
                if process.terminationStatus != 0 {
                    self?.showGeminiConfigError(stderr)
                } else {
                    self?.showGeminiConfigSuccess()
                }
            }
        }
    }

    func showGeminiConfigError(_ stderr: String) {
        let detail = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        let alert = NSAlert()
        alert.messageText = "Could not save Gemini key"
        alert.informativeText = detail.isEmpty
            ? "agent-doctor settings set-gemini-key failed."
            : short(detail, 256)
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        if let window = self.window {
            alert.beginSheetModal(for: window)
        } else {
            alert.window.makeKeyAndOrderFront(nil)
        }
    }

    func showGeminiConfigSuccess() {
        let alert = NSAlert()
        alert.messageText = "Gemini key saved"
        alert.informativeText = "Use Generate sprite from prompt... to make a new pet."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        if let window = self.window {
            alert.beginSheetModal(for: window)
        } else {
            alert.window.makeKeyAndOrderFront(nil)
        }
    }

    @objc func changeSprite(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.title = "Change Agent Doctor Sprite"
        panel.message = "Choose a PNG, JPEG, GIF, or WebP image."
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if #available(macOS 11.0, *) {
            let webp = UTType(filenameExtension: "webp") ?? UTType.image
            panel.allowedContentTypes = [.png, .jpeg, .gif, webp]
        } else {
            panel.allowedFileTypes = ["png", "jpg", "jpeg", "gif", "webp"]
        }
        panel.begin { [weak self] response in
            guard response == .OK, let selectedURL = panel.url else {
                return
            }
            self?.setSprite(selectedURL.path)
        }
    }

    @objc func resetSprite(_ sender: Any?) {
        if !userSpritePath.isEmpty {
            try? FileManager.default.removeItem(atPath: userSpritePath)
            reloadSpriteIfChanged()
        }
    }

    @objc func closePetFromMenu(_ sender: Any?) {
        quitPet()
    }

    func setSprite(_ selectedPath: String) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: pythonExecutable)
            process.arguments = [
                "-m",
                "agent_doctor.cli",
                "pet-set-sprite",
                selectedPath
            ]
            let errorPipe = Pipe()
            let errorCollector = ProcessOutputCollector(errorPipe)
            process.standardError = errorPipe
            do {
                try process.run()
                process.waitUntilExit()
            } catch {
                DispatchQueue.main.async {
                    self?.showSpriteError(error.localizedDescription)
                }
                return
            }
            let stderr = errorCollector.finish()
            DispatchQueue.main.async {
                if process.terminationStatus != 0 {
                    self?.showSpriteError(stderr)
                } else {
                    self?.reloadSpriteIfChanged()
                }
            }
        }
    }

    func showSpriteError(_ stderr: String) {
        let detail = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        let alert = NSAlert()
        alert.messageText = "Could not change sprite"
        alert.informativeText = detail.isEmpty
            ? "agent-doctor pet-set-sprite failed."
            : short(detail, 256)
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        if let window = self.window {
            alert.beginSheetModal(for: window)
        } else {
            alert.window.makeKeyAndOrderFront(nil)
        }
    }

    @objc func muteForNow(_ sender: Any?) {
        bubbleOpen = false
        if deliveryResultActive() && !deliveryEventId.isEmpty {
            dismissedEventId = deliveryEventId
        } else {
            dismissedEventId = currentEventKey()
        }
        clearDeliveryResult()
        needsDisplay = true
        displayIfNeeded()
        persistDismissCurrentIncident()
    }

    @objc func openStatusCard(_ sender: Any?) {
        let path = status["card_path"] ?? ""
        if !path.isEmpty {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        } else {
            bubbleOpen = true
            noticeText = "No status card is available for this state."
            needsDisplay = true
        }
    }

    func performAction(_ actionId: String) {
        if actionId == "tell_current_agent" {
            sendRecoveryToAgent(nil)
            return
        }
        if actionId == "open_card" {
            openStatusCard(nil)
            return
        }
        if actionId == "dismiss_for_now" {
            muteForNow(nil)
            return
        }
        if actionId == "quit_pet" {
            quitPet()
            return
        }
        runOptionCommand(actionId)
    }

    func runOptionCommand(_ optionId: String) {
        if !runningActionId.isEmpty {
            bubbleOpen = true
            noticeText = actionBusyText(runningActionId)
            needsDisplay = true
            return
        }
        let command = optionValue(optionId, "command", "")
        if !isRunnableCommand(command) {
            bubbleOpen = true
            noticeText = "This action is not available for the current state."
            needsDisplay = true
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-lc", command]
        let output = Pipe()
        let outputCollector = ProcessOutputCollector(output)
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self, weak process] completed in
            let text = outputCollector.finish()
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let process = process {
                    self.activeProcesses.removeAll { $0 === process }
                }
                self.runningActionId = ""
                if completed.terminationStatus == 0 {
                    self.requestStatusReload(Date())
                    self.noticeText = self.actionFinishedText(optionId)
                } else {
                    self.noticeText = self.actionFailedText(optionId, text)
                }
                self.bubbleOpen = true
                self.needsDisplay = true
            }
        }
        runningActionId = optionId
        noticeText = actionStartedText(optionId)
        bubbleOpen = true
        needsDisplay = true
        do {
            try process.run()
            activeProcesses.append(process)
        } catch {
            noticeText = error.localizedDescription
            runningActionId = ""
            bubbleOpen = true
            needsDisplay = true
            return
        }
    }

    func persistDismissCurrentIncident() {
        if statusPath.isEmpty {
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonExecutable)
        process.arguments = [
            "-m",
            "agent_doctor.cli",
            "pet-action",
            "dismiss",
            "--status-file",
            statusPath
        ]
        let output = Pipe()
        let outputCollector = ProcessOutputCollector(output)
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self, weak process] completed in
            _ = outputCollector.finish()
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let process = process {
                    self.activeProcesses.removeAll { $0 === process }
                }
                if completed.terminationStatus == 0 {
                    self.requestStatusReload(Date())
                }
                self.needsDisplay = true
            }
        }
        do {
            try process.run()
            activeProcesses.append(process)
        } catch {
            return
        }
    }

    @objc func sendRecoveryToAgent(_ sender: Any?) {
        if !runningActionId.isEmpty {
            bubbleOpen = true
            noticeText = actionBusyText(runningActionId)
            needsDisplay = true
            return
        }
        let snapshotPath: String
        do {
            snapshotPath = try writeCurrentStatusSnapshot(status)
        } catch {
            bubbleOpen = true
            setDeliveryResult(false, error.localizedDescription)
            needsDisplay = true
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonExecutable)
        process.arguments = [
            "-m",
            "agent_doctor.cli",
            "pet-action",
            "send-recovery",
            "--status-file",
            snapshotPath
        ]
        let output = Pipe()
        let outputCollector = ProcessOutputCollector(output)
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self, weak process] completed in
            let text = outputCollector.finish()
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let process = process {
                    self.activeProcesses.removeAll { $0 === process }
                }
                try? FileManager.default.removeItem(atPath: snapshotPath)
                self.runningActionId = ""
                let detail = self.actionDetail(text)
                if completed.terminationStatus == 0 {
                    self.dismissedEventId = self.currentEventKey()
                    self.setDeliveryResult(true, self.deliverySuccessText(detail))
                    self.persistDismissCurrentIncident()
                } else {
                    self.setDeliveryResult(false, self.deliveryFailureText(detail))
                }
                self.needsDisplay = true
            }
        }
        runningActionId = "tell_current_agent"
        noticeText = actionStartedText("tell_current_agent")
        bubbleOpen = true
        needsDisplay = true
        do {
            try process.run()
            activeProcesses.append(process)
        } catch {
            runningActionId = ""
            try? FileManager.default.removeItem(atPath: snapshotPath)
            bubbleOpen = true
            setDeliveryResult(false, error.localizedDescription)
            needsDisplay = true
            return
        }
    }

    func quitPet() {
        NSApplication.shared.terminate(nil)
    }

    func isRunnableCommand(_ command: String) -> Bool {
        let trimmed = command.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmed.isEmpty && !trimmed.contains("<") && !trimmed.contains(">")
    }

    func actionDetail(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else {
            return short(trimmed.replacingOccurrences(of: "\n", with: " "), 360)
        }
        let detail = stringValue(dict, "detail", "")
        if !detail.isEmpty {
            return detail
        }
        return stringValue(dict, "mode", "")
    }

    func hostProductName() -> String {
        let platform = status["platform"] ?? ""
        if platform == "openclaw" {
            return "OpenClaw"
        }
        return "OpenClaw"
    }

    func deliverySuccessText(_ detail: String) -> String {
        let app = hostProductName()
        let technical = short(detail.trimmingCharacters(in: .whitespacesAndNewlines), 100)
        if useChinese() {
            let base = "已把恢复建议发送给当前 \(app) Agent。请回到会话，查看它是否已经停止当前错误路径并开始修复。"
            return technical.isEmpty ? base : "\(base)\n\(technical)"
        }
        let base = "Sent the recovery suggestion to the active \(app) agent. Return to the session and check whether it stops the failing path and starts recovering."
        return technical.isEmpty ? base : "\(base)\n\(technical)"
    }

    func deliveryFailureText(_ detail: String) -> String {
        let technical = short(detail.trimmingCharacters(in: .whitespacesAndNewlines), 120)
        if useChinese() {
            let base = "Agent Doctor 还没有把建议送到当前 Agent。OpenClaw 路由不可用或定向发送失败。"
            return technical.isEmpty ? base : "\(base)\n\(technical)"
        }
        let base = "Agent Doctor has not sent the suggestion to the active agent. OpenClaw routing is unavailable or targeted delivery failed."
        return technical.isEmpty ? base : "\(base)\n\(technical)"
    }

    func setDeliveryResult(_ succeeded: Bool, _ text: String) {
        deliveryResultSucceeded = succeeded
        deliveryResultText = text
        deliveryResultUntil = Date().addingTimeInterval(90)
        deliveryEventId = currentEventKey()
        noticeText = ""
        bubbleOpen = true
    }

    func clearDeliveryResult() {
        deliveryResultText = ""
        deliveryResultSucceeded = false
        deliveryResultUntil = Date(timeIntervalSince1970: 0)
        deliveryEventId = ""
    }

    func optionValue(_ optionId: String, _ key: String, _ fallback: String) -> String {
        let count = Int(status["option_count"] ?? "0") ?? 0
        for index in 0..<count {
            if status["option_\(index)_id"] == optionId {
                let value = status["option_\(index)_\(key)"] ?? ""
                return value.isEmpty ? fallback : value
            }
        }
        return fallback
    }

    func displayActions() -> [String] {
        var actions: [String] = []
        var seen = Set<String>()
        if deliveryResultActive() {
            actions.append("dismiss_for_now")
            return actions
        }
        let state = status["state"] ?? "idle"
        if state == "concerned" || state == "intervening" {
            actions.append("dismiss_for_now")
            return actions
        }
        let count = Int(status["option_count"] ?? "0") ?? 0
        for index in 0..<count {
            let optionId = status["option_\(index)_id"] ?? ""
            let command = status["option_\(index)_command"] ?? ""
            if optionId == "start_autopilot" {
                continue
            }
            if !optionId.isEmpty && !seen.contains(optionId) && isRunnableCommand(command) {
                actions.append(optionId)
                seen.insert(optionId)
            }
        }
        actions.append("dismiss_for_now")
        actions.append("quit_pet")
        if !(status["card_path"] ?? "").isEmpty {
            actions.append("open_card")
        }
        return actions
    }

    func visibleActions() -> [String] {
        return Array(displayActions().prefix(6))
    }

    func performButton(at point: NSPoint) -> Bool {
        for (actionId, rect) in buttonFrames.reversed() {
            if rect.contains(point) {
                performAction(actionId)
                return true
            }
        }
        return false
    }

    func canSendRecovery() -> Bool {
        if incidentExpired() {
            return false
        }
        let platform = status["platform"] ?? "generic"
        if platform != "openclaw" {
            return false
        }
        let state = status["state"] ?? "idle"
        if state != "concerned" && state != "intervening" {
            return false
        }
        let file = status["evidence_0_file"] ?? ""
        return !file.isEmpty && file != "<manual>"
    }

    func actionTitle(_ actionId: String) -> String {
        let chinese = useChinese()
        if actionId == runningActionId {
            return chinese ? "处理中..." : "Working..."
        }
        if actionId == "tell_current_agent" {
            return chinese ? "发送给当前 Agent" : "Tell Current Agent"
        }
        if actionId == "open_card" {
            return chinese ? "打开详情" : "Open Card"
        }
        if actionId == "dismiss_for_now" {
            if deliveryResultActive() {
                return chinese ? "知道了" : "Done"
            }
            let state = status["state"] ?? "idle"
            if state == "concerned" || state == "intervening" {
                return chinese ? "知道了" : "Got it"
            }
            return chinese ? "关闭" : "Close"
        }
        if actionId == "quit_pet" {
            return chinese ? "退出" : "Quit"
        }
        return optionValue(actionId, "label", "Run Action")
    }

    func actionStartedText(_ actionId: String) -> String {
        return "\(actionTitle(actionId)) started."
    }

    func actionFinishedText(_ actionId: String) -> String {
        return "\(actionTitle(actionId)) finished."
    }

    func actionFailedText(_ actionId: String, _ output: String) -> String {
        let detail = short(output.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "\n", with: " "), 120)
        return detail.isEmpty ? "\(actionTitle(actionId)) failed." : "\(actionTitle(actionId)) failed: \(detail)"
    }

    func actionBusyText(_ actionId: String) -> String {
        return "Still running \(actionTitle(actionId))..."
    }

    func containsCJK(_ value: String) -> Bool {
        for scalar in value.unicodeScalars {
            if scalar.value >= 0x4e00 && scalar.value <= 0x9fff {
                return true
            }
        }
        return false
    }

    func useChinese() -> Bool {
        return containsCJK([
            status["headline"] ?? "",
            status["message"] ?? "",
            status["emotion_message"] ?? "",
            status["diagnosis"] ?? "",
            status["recommendation"] ?? "",
            status["evidence_0_quote"] ?? ""
        ].joined(separator: "\n"))
    }

    func issueTitle() -> String {
        let headline = status["headline"] ?? ""
        if !headline.isEmpty {
            return headline
        }
        let trigger = status["latest_trigger"] ?? ""
        let chinese = useChinese()
        if trigger == "user_frustration_signal" {
            return chinese ? "检测到用户不满" : "User Frustration Detected"
        }
        if trigger == "completion_claim_without_nearby_verification" {
            return chinese ? "完成声明需要验证" : "Completion Claim Needs Verification"
        }
        if trigger == "tool_failure_or_hidden_error" {
            return chinese ? "工具失败需要处理" : "Tool Failure Needs Acknowledgement"
        }
        return status["headline"] ?? "Agent Doctor"
    }

    func deliveryResultActive() -> Bool {
        return !deliveryResultText.isEmpty && Date() <= deliveryResultUntil
    }

    func deliveryPanelTitle() -> String {
        if deliveryResultSucceeded {
            return useChinese() ? "已发送给当前 Agent" : "Sent to active agent"
        }
        return useChinese() ? "发送失败" : "Could not send"
    }

    func deliveryPanelHelper() -> String {
        if deliveryResultSucceeded {
            return useChinese()
                ? "现在回到 OpenClaw，确认当前 Agent 是否按建议恢复。Agent Doctor 会继续监控新的用户反馈。"
                : "Return to OpenClaw and confirm the agent recovers. Agent Doctor will keep watching for new feedback."
        }
        return useChinese()
            ? "自动发送没有成功。请忽略这次提醒，Agent Doctor 会继续监控新的反馈。"
            : "Automatic delivery did not complete. Dismiss this alert; Agent Doctor will keep watching for new feedback."
    }

    func panelTitle(_ state: String) -> String {
        return issueTitle()
    }

    func panelDiagnosisText(_ state: String) -> String {
        let diagnosis = status["diagnosis"] ?? ""
        return diagnosis.isEmpty ? (status["message"] ?? "") : diagnosis
    }

    func panelNextStepText(_ state: String) -> String {
        return expectationText()
    }

    func idleSummaryText() -> String {
        if status["headline"] == "Current session checked." {
            return "No quality signal found in the latest supported session."
        }
        return "No active incident. Agent Doctor is watching supported sessions."
    }

    func evidenceText() -> String {
        let count = Int(status["evidence_count"] ?? "0") ?? 0
        let chinese = useChinese()
        if count == 0 {
            return chinese ? "当前状态没有包含 transcript 证据。" : "No transcript evidence was included in this status."
        }
        if status["latest_trigger"] == "tool_failure_or_hidden_error" {
            return chinese ? "工具输出里出现了失败或错误信号。" : "Tool output contains failure or error language."
        }
        let role = status["evidence_0_role"] ?? ""
        let quote = short(status["evidence_0_quote"] ?? "", 180)
        let file = status["evidence_0_file"] ?? ""
        let line = status["evidence_0_line"] ?? ""
        var source = file.isEmpty || file == "<manual>" ? "Manual report" : file
        if !line.isEmpty && line != "0" && !file.isEmpty && file != "<manual>" {
            source = source.isEmpty ? "line \(line)" : "\(source):\(line)"
        }
        let speaker = role.isEmpty ? "Evidence" : role.prefix(1).uppercased() + role.dropFirst()
        return "\(speaker) quote: \"\(quote)\"\nSource: \(source)"
    }

    func expectationText() -> String {
        let recommendation = status["recommendation"] ?? ""
        if !recommendation.isEmpty {
            return recommendation
        }
        let trigger = status["latest_trigger"] ?? ""
        if trigger == "user_frustration_signal" {
            return "The active agent should stop the normal success path, acknowledge the concrete failure, and give one evidence-backed recovery step."
        }
        if trigger == "completion_claim_without_nearby_verification" {
            return "The active agent should verify the claim before repeating success or saying the work is done."
        }
        if trigger == "tool_failure_or_hidden_error" {
            return "The active agent should surface the tool failure and adjust the plan before claiming progress."
        }
        return status["message"] ?? "Review the concrete evidence before changing the current response."
    }

    func userActionText() -> String {
        let state = status["state"] ?? "idle"
        if state == "concerned" || state == "intervening" {
            if useChinese() {
                return "不用操作。点“知道了”会收起这次安慰；如果你不点，它会在 \(status["expires_after_seconds"] ?? "120") 秒后自己安静退下。"
            }
            return "No action is needed. Got it hides this comfort moment; otherwise it fades after \(status["expires_after_seconds"] ?? "120") seconds."
        }
        if !(status["card_path"] ?? "").isEmpty {
            return "Open the status card for details, or hide this alert after you have seen it."
        }
        return "Agent Doctor is monitoring automatically. No manual session check is needed."
    }

    func detailText(_ state: String, _ action: String) -> String {
        var details = ""
        if let emotion = status["emotion_message"], !emotion.isEmpty {
            details += "\(emotion)\n\n"
        }
        details += "Status: \(stateLabel(state, action))"
        if let session = status["session_id"], !session.isEmpty {
            details += "\nSession: \(session)"
        }
        let diagnosis = status["diagnosis"] ?? ""
        details += "\n\nDiagnosis:\n\(diagnosis.isEmpty ? (status["message"] ?? "") : diagnosis)"
        details += "\n\nEvidence:\n\(evidenceText())"
        details += "\n\nSuggested next step:\n\(expectationText())"
        details += "\n\nYour choices:\n\(userActionText())"
        return details
    }

    func recoveryPrompt() -> String {
        let prompt = status["recovery_prompt"] ?? ""
        if !prompt.isEmpty {
            return prompt
        }
        return [
            "Agent Doctor detected a live quality issue.",
            "",
            "Concrete evidence:",
            evidenceText(),
            "",
            "Do this now:",
            expectationText(),
            "",
            "Do not continue the normal success path until the failure is acknowledged and the next corrective step is clear."
        ].joined(separator: "\n")
    }

    func currentEventKey() -> String {
        let eventId = status["latest_event_id"] ?? ""
        if !eventId.isEmpty {
            return eventId
        }
        return "\(status["state"] ?? "")|\(status["session_id"] ?? "")|\(status["headline"] ?? "")"
    }

    func observeCurrentEvent() {
        let key = currentEventKey()
        if activeEventId != key {
            activeEventId = key
            eventFirstSeenAt = Date()
            let state = status["state"] ?? "idle"
            if (state == "concerned" || state == "intervening") && key != dismissedEventId {
                clearDeliveryResult()
            }
            if dismissedEventId != key && !deliveryResultActive() {
                bubbleOpen = false
            }
        }
    }

    func incidentExpired() -> Bool {
        let state = status["state"] ?? "idle"
        if state != "concerned" && state != "intervening" {
            return false
        }
        let seconds = Double(status["expires_after_seconds"] ?? "120") ?? 120
        if seconds <= 0 {
            return false
        }
        return Date().timeIntervalSince(eventFirstSeenAt) >= seconds
    }

    func shouldAutoShowBubble(_ state: String) -> Bool {
        if state != "concerned" && state != "intervening" {
            return false
        }
        if incidentExpired() {
            return false
        }
        return currentEventKey() != dismissedEventId
    }

    func panelVisible(_ state: String) -> Bool {
        if deliveryResultActive() {
            return true
        }
        return bubbleOpen || shouldAutoShowBubble(state)
    }

    func isIncidentStatus(_ payload: [String: String]) -> Bool {
        let state = payload["state"] ?? "idle"
        return state == "concerned" || state == "intervening"
    }

    func shouldKeepCurrentIncident(_ nextStatus: [String: String]) -> Bool {
        if !isIncidentStatus(status) || isIncidentStatus(nextStatus) {
            return false
        }
        if currentEventKey() == dismissedEventId {
            return false
        }
        return !incidentExpired()
    }

    func requestStatusReload(_ now: Date) {
        if statusReloadInFlight {
            return
        }
        statusReloadInFlight = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let nextStatus = loadStatus()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.statusReloadInFlight = false
                self.applyStatusReload(nextStatus, now)
            }
        }
    }

    func applyStatusReload(_ nextStatus: [String: String], _ now: Date) {
        if shouldKeepCurrentIncident(nextStatus) {
            lastStatusReload = now
            needsDisplay = true
            return
        }
        status = nextStatus
        lastStatusReload = now
    }

    func syncWindowSize(expanded: Bool, state: String) {
        guard let window = self.window else { return }
        let width = expanded ? expandedWindowWidth : compactWindowWidth
        var height = expanded ? expandedWindowHeight : compactWindowHeight
        if expanded && state == "idle" && !deliveryResultActive() {
            height = noticeText.isEmpty ? idleExpandedWindowHeight : idleNoticeExpandedWindowHeight
        }
        let frame = window.frame
        if abs(frame.width - width) < 0.5 && abs(frame.height - height) < 0.5 {
            return
        }
        let next = NSRect(
            x: frame.maxX - width,
            y: frame.maxY - height,
            width: width,
            height: height
        )
        window.setFrame(next, display: true)
        self.frame = NSRect(x: 0, y: 0, width: width, height: height)
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
        let options: NSString.DrawingOptions = [
            .usesLineFragmentOrigin,
            .usesFontLeading,
            .truncatesLastVisibleLine
        ]
        NSString(string: value).draw(with: r(x, y, w, h), options: options, attributes: attrs)
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
            let size = 106 + (16 * p)
            oval(95 - size / 2, 86 - size / 2, size, size, color("#fff7ed").withAlphaComponent(0.18), color("#f97316").withAlphaComponent(0.45), 2)
            oval(52, 40, 86, 86, color("#fde68a").withAlphaComponent(0.12), NSColor.clear, 0)
            for index in 0..<5 {
                let fi = CGFloat(index)
                let bob = CGFloat(8.0 * pulse(t + Double(index), 2.2 + Double(index) * 0.35))
                let x = CGFloat([28, 48, 136, 150, 72][index])
                let y = CGFloat([42, 122, 36, 116, 20][index]) + bob
                let c = [color("#fde68a"), color("#14b8a6"), color("#f97316"), color("#fca5a5"), color("#ffffff")][index]
                oval(x, y, 10 + (fi.truncatingRemainder(dividingBy: 2) * 4), 10 + (fi.truncatingRemainder(dividingBy: 2) * 4), c.withAlphaComponent(0.82), color("#111827").withAlphaComponent(0.55), 1)
            }
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
        roundRect(50, 72 - lift, 82, 47, 13, color("#f4fff7"), color("#375b71"), 3)
        let eye = state == "intervening" ? NSColor.white : color("#348b88")
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
            roundRect(142, 25 + (4 * p), 30, 30, 15, color("#f97316"), .white, 2)
            text("~", 142, 28 + (4 * p), 30, 22, 17, .white, true)
            oval(142 - (6 * p), 25 - (6 * p), 30 + (12 * p), 30 + (12 * p), NSColor.clear, color("#f97316").withAlphaComponent(0.45), 2)
        }
    }

    func short(_ value: String, _ limit: Int) -> String {
        if value.count <= limit {
            return value
        }
        let end = value.index(value.startIndex, offsetBy: max(0, limit - 1))
        return String(value[..<end]) + "..."
    }

    func stateLabel(_ state: String, _ action: String) -> String {
        let chinese = useChinese()
        if state != "idle" {
            let phase = status["phase"] ?? ""
            if phase == "comforting" {
                return chinese ? "小医生陪着" : "Comforting"
            }
            if phase == "advice_ready" {
                return chinese ? "建议已准备" : "Suggestion ready"
            }
            if phase == "diagnosing" {
                return chinese ? "诊断中" : "Diagnosing"
            }
        }
        if state == "intervening" {
            return chinese ? "需要处理" : "Intervention needed"
        }
        if state == "concerned" {
            return chinese ? "需要查看" : "Needs review"
        }
        if state == "watching" {
            return chinese ? "监控中" : "Watching"
        }
        return chinese ? "健康" : "Idle"
    }

    func drawStateChip(_ state: String, _ action: String, _ accent: NSColor) {
        if state == "idle" {
            return
        }
        roundRect(28, 270, 204, 30, 15, NSColor.white.withAlphaComponent(0.96), accent, 2)
        oval(43, 281, 10, 10, accent, NSColor.clear, 0)
        text(stateLabel(state, action), 64, 276, 152, 18, 11, color("#111827"), true, .left)
    }

    func drawActionButton(_ actionId: String, _ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ primary: Bool, _ accent: NSColor) {
        let busy = actionId == runningActionId
        let fill = busy ? accent : (primary ? color("#f97316") : NSColor.white.withAlphaComponent(0.92))
        let stroke = busy ? accent : (primary ? color("#f97316") : color("#d1d5db"))
        let foreground = primary ? NSColor.white : color("#111827")
        let rect = r(x, y, w, h)
        roundRect(x, y, w, h, h / 2, fill, stroke, 1)
        text(actionTitle(actionId), x + 8, y + 7, w - 16, h - 12, 10.5, busy ? NSColor.white : foreground, primary || busy)
        buttonFrames.append((actionId, rect))
    }

    func drawIdlePanel(_ accent: NSColor) {
        let hasNotice = !noticeText.isEmpty
        let panelHeight: CGFloat = hasNotice ? 250 : 184
        let primaryY: CGFloat = hasNotice ? 378 : 346
        roundRect(18, 210, 324, panelHeight, 22, NSColor.white.withAlphaComponent(0.96), color("#111827"), 1.5)
        text(short(panelTitle("idle"), 82), 36, 232, 288, 36, 13.5, color("#111827"), true, .left)
        text(short(idleSummaryText(), 120), 36, 282, 288, 42, 11.5, color("#374151"), false, .left)

        if hasNotice {
            roundRect(34, 326, 292, 40, 12, accent.withAlphaComponent(0.10), accent.withAlphaComponent(0.28), 1)
            text(short(noticeText, 96), 48, 335, 264, 22, 10.5, accent, true, .left)
        }

        let actions = visibleActions()
        if actions.count == 1 {
            drawActionButton(actions[0], 36, primaryY, 288, 30, false, accent)
        } else {
            let idleActions = Array(actions.prefix(4))
            for (index, actionId) in idleActions.enumerated() {
                let col = index % 2
                let row = index / 2
                drawActionButton(actionId, col == 0 ? 36 : 186, primaryY + (CGFloat(row) * 34), 138, 28, false, accent)
            }
        }
    }

    func drawDeliveryResultPanel(_ state: String, _ accent: NSColor) {
        let chinese = useChinese()
        let statusColor = deliveryResultSucceeded ? color("#16a34a") : color("#dc2626")
        let softFill = deliveryResultSucceeded ? color("#dcfce7") : color("#fee2e2")
        roundRect(18, 210, 324, 340, 22, NSColor.white.withAlphaComponent(0.96), color("#111827"), 1.5)
        roundRect(36, 230, 138, 24, 12, softFill.withAlphaComponent(0.72), statusColor, 1)
        text(deliveryResultSucceeded ? (chinese ? "已发送" : "Sent") : (chinese ? "需要手动处理" : "Needs manual send"), 48, 236, 114, 13, 9.5, statusColor, true, .left)
        text(short(deliveryPanelTitle(), 72), 36, 268, 288, 34, 13.5, color("#111827"), true, .left)

        text(chinese ? "发生了什么" : "What happened", 36, 316, 288, 14, 10, color("#111827"), true, .left)
        text(short(deliveryResultText, 190), 36, 334, 288, 82, 10.5, color("#374151"), false, .left)
        text(chinese ? "下一步" : "Next step", 36, 430, 288, 14, 10, color("#111827"), true, .left)
        text(short(deliveryPanelHelper(), 130), 36, 448, 288, 38, 10.5, color("#374151"), false, .left)

        let actions = visibleActions()
        let rowY: CGFloat = 504
        if actions.count == 1 {
            drawActionButton(actions[0], 36, rowY, 288, 30, true, accent)
        } else if actions.count == 2 {
            drawActionButton(actions[0], 36, rowY, 138, 30, true, accent)
            drawActionButton(actions[1], 186, rowY, 138, 30, false, accent)
        }
    }

    func drawPanel(_ state: String, _ accent: NSColor) {
        buttonFrames.removeAll()
        guard panelVisible(state) else {
            return
        }
        if deliveryResultActive() {
            drawDeliveryResultPanel(state, accent)
            return
        }
        if state == "idle" {
            drawIdlePanel(accent)
            return
        }
        roundRect(18, 210, 324, 340, 22, color("#fff7ed").withAlphaComponent(0.98), color("#111827"), 1.5)
        let chinese = useChinese()
        roundRect(36, 228, 116, 24, 12, color("#fde68a").withAlphaComponent(0.75), color("#f97316"), 1)
        text(chinese ? "小医生来了" : "Tiny doctor here", 48, 234, 92, 13, 9.5, color("#9a3412"), true, .left)
        text(short(panelTitle(state), 58), 36, 266, 288, 40, 15, color("#111827"), true, .left)

        var y: CGFloat = 320
        let emotion = status["emotion_message"] ?? ""
        if !emotion.isEmpty {
            roundRect(34, y - 8, 292, 92, 18, NSColor.white.withAlphaComponent(0.78), color("#f97316").withAlphaComponent(0.35), 1)
            text(short(emotion, 190), 50, y + 6, 260, 66, 12, color("#374151"), false, .left)
            y += 104
        }
        text(chinese ? "它看到的现场" : "What it noticed", 40, y, 288, 14, 10, color("#111827"), true, .left)
        text(short(evidenceText(), 128), 40, y + 18, 270, 48, 10.5, color("#6b7280"), false, .left)

        if !noticeText.isEmpty {
            roundRect(176, 228, 132, 24, 12, color("#14b8a6").withAlphaComponent(0.10), color("#14b8a6").withAlphaComponent(0.28), 1)
            text(short(noticeText, 42), 186, 234, 112, 12, 9.5, color("#0f766e"), true, .left)
        }

        let actions = visibleActions()
        let rowY: CGFloat = 512
        if actions.count == 1 {
            drawActionButton(actions[0], 36, rowY, 288, 30, true, accent)
        } else if actions.count == 2 {
            drawActionButton(actions[0], 36, rowY, 138, 30, true, accent)
            drawActionButton(actions[1], 186, rowY, 138, 30, false, accent)
        } else {
            for (index, actionId) in actions.enumerated() {
                let row = CGFloat(index / 2)
                let col = index % 2
                drawActionButton(actionId, col == 0 ? 36 : 186, rowY + (row * 34), 138, 28, index == 0, accent)
            }
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        observeCurrentEvent()
        let rawState = status["state"] ?? "idle"
        let state = incidentExpired() ? "idle" : rawState
        let (_, accent, glow) = palette(state)
        let t = Date().timeIntervalSince(startedAt)
        let shadowPulse = 1.0 + (0.08 * pulse(t, 2.0))
        let expanded = panelVisible(state)
        syncWindowSize(expanded: expanded, state: state)

        NSGraphicsContext.saveGraphicsState()
        let transform = NSAffineTransform()
        let petXOffset = ((bounds.width - compactWindowWidth) / 2.0) + 35
        let petYOffset: CGFloat = expanded ? 0 : 88
        transform.translateX(by: petXOffset, yBy: -petYOffset)
        transform.concat()
        drawEffects(state, t, accent, glow)
        oval(57 - (3 * shadowPulse), 180, 76 + (6 * shadowPulse), 16, color("#111827").withAlphaComponent(0.22), NSColor.clear, 0)
        drawSprite(state, t)
        drawOverlays(state, t, accent, glow)
        NSGraphicsContext.restoreGraphicsState()
        if state != "idle" && !expanded {
            drawStateChip(state, status["action"] ?? "silent", accent)
        }
        drawPanel(state, accent)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

// Install a minimal main menu so standard Edit shortcuts (Cmd-X / Cmd-C /
// Cmd-V / Cmd-A) reach the text fields in our NSAlert dialogs. With the
// `.accessory` activation policy AppKit installs no Edit menu by default,
// so key equivalents inside modal alerts have nothing to validate against
// and silently no-op — pasting a Gemini API key into "Configure Gemini..."
// or a prompt into "Generate sprite from prompt..." fails. Routing through
// the standard NSText.* selectors lets whichever NSTextField/NSSecureTextField
// is firstResponder pick them up automatically.
let editMenu = NSMenu(title: "Edit")
editMenu.addItem(withTitle: "Cut",        action: #selector(NSText.cut(_:)),       keyEquivalent: "x")
editMenu.addItem(withTitle: "Copy",       action: #selector(NSText.copy(_:)),      keyEquivalent: "c")
editMenu.addItem(withTitle: "Paste",      action: #selector(NSText.paste(_:)),     keyEquivalent: "v")
editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
let mainMenu = NSMenu()
let editMenuItem = NSMenuItem()
editMenuItem.submenu = editMenu
mainMenu.addItem(editMenuItem)
app.mainMenu = mainMenu

let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
let startFrame = NSRect(
    x: screenFrame.maxX - compactWindowWidth - 80,
    y: screenFrame.maxY - compactWindowHeight - 80,
    width: compactWindowWidth,
    height: compactWindowHeight
)

let window = NSWindow(
    contentRect: startFrame,
    styleMask: [.borderless],
    backing: .buffered,
    defer: false
)
window.title = "Agent Doctor"
window.isReleasedWhenClosed = false
window.isOpaque = false
window.backgroundColor = .clear
window.hasShadow = false
window.isMovableByWindowBackground = true
window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
if topmost {
    window.level = .floating
}

let view = PetView(frame: NSRect(x: 0, y: 0, width: compactWindowWidth, height: compactWindowHeight))
view.wantsLayer = true
view.layer?.backgroundColor = NSColor.clear.cgColor
view.autoresizingMask = [.width, .height]
window.contentView = view
// Initial sprite load goes through currentSpritePath() so the first paint
// already reflects the user override when present, rather than trusting
// whichever single path Python resolved at launch.
view.reloadSpriteIfChanged()
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)

Timer.scheduledTimer(withTimeInterval: 1.0 / 15.0, repeats: true) { _ in
    let now = Date()
    if now.timeIntervalSince(view.lastStatusReload) >= max(0.2, pollSeconds) {
        // Sprite changes are user-driven and rare; check at the status-poll
        // cadence (default 1 s) instead of every animation tick.
        view.reloadSpriteIfChanged()
        view.requestStatusReload(now)
    } else {
        view.needsDisplay = true
    }
}

app.run()
