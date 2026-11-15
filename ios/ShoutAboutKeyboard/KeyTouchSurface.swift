import UIKit

/// What the touch surface reports (the keyboard's controller).
@MainActor
protocol KeyTouchSurfaceDelegate: AnyObject {
    func keyDown(_ key: KeyboardKey)
    /// A finger slid from one character key to another before lifting.
    func keyMoved(from old: KeyboardKey, to new: KeyboardKey)
    /// The finger lifted: type it (or finish what the key does).
    func keyUp(_ key: KeyboardKey)
    /// The touch ended without typing (cancelled, or the space bar moved the cursor).
    func keyCancelled(_ key: KeyboardKey)
    /// Dragging on the space bar (or holding it) turned the keys into a trackpad for the
    /// cursor; it ends with `keyCancelled(.space)`.
    func cursorDragBegan()
    /// Side to side: the cursor moves this many characters (negative: back).
    func moveCursor(by characters: Int)
    /// Up and down: this many lines (negative: up).
    func moveCursor(lines: Int)
    func panelTapped()
    func panelCancelTapped()
    /// iOS's keyboard switcher wants the globe's touches with their event.
    func globeTouch(from view: UIView, with event: UIEvent?)
    /// VoiceOver activated the globe.
    func advanceToNextKeyboard()
    func spokenName(for key: KeyboardKey) -> String
}

/// One clear view over the whole keyboard that takes every touch. It finds the key from the
/// shared `KeyboardLayout` frames (no dead spots between keys), types when the finger lifts like
/// the iPhone keyboard, lets thumbs overlap (a new letter commits the one still held), turns the
/// space bar into a trackpad for the cursor (drag it, or touch and hold it, then move in any
/// direction), and on the listening panel turns any tap into "finish" (Cancel apart). A touch
/// that began on a key stays that key's until it lifts, even if the panel appears meanwhile
/// (holding the mouth to talk).
final class KeyTouchSurface: UIView {
    weak var delegate: KeyTouchSurfaceDelegate?

    private(set) var frames: [KeyboardKeyFrame] = []
    private(set) var keyAreaTop: CGFloat = 0
    private(set) var panelMode = false

    private struct KeyTouch {
        var key: KeyboardKey
        var start: CGPoint
        var last: CGPoint
        /// The space bar is a trackpad: moves go to the cursor, nothing is typed.
        var cursorMode = false
        /// Where the cursor's last step was taken from.
        var anchor: CGPoint = .zero
    }

    private enum Tracked {
        case key(KeyTouch)
        case panel(startedOnCancel: Bool)
    }

    private var tracked: [ObjectIdentifier: Tracked] = [:]
    /// Character touches still down, oldest first.
    private var heldCharacters: [ObjectIdentifier] = []
    private var spaceHold: Task<Void, Never>?

    /// Points of drag per character the cursor moves along a line.
    private static let cursorStep: CGFloat = 9
    /// Points of drag per line the cursor moves up or down.
    private static let lineStep: CGFloat = 20
    /// A drag this far on the space bar starts the trackpad.
    private static let dragToTrackpad: CGFloat = 14
    /// So does holding the space bar this long (as on the iPhone).
    private static let holdToTrackpad: Duration = .milliseconds(450)

    override init(frame: CGRect) {
        super.init(frame: frame)
        isMultipleTouchEnabled = true
        backgroundColor = .clear
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not used")
    }

    func update(frames: [KeyboardKeyFrame], keyAreaTop: CGFloat, panelMode: Bool) {
        let changed = frames != self.frames || keyAreaTop != self.keyAreaTop || panelMode != self.panelMode
        self.frames = frames
        self.keyAreaTop = keyAreaTop
        self.panelMode = panelMode
        if changed { refreshAccessibility() }
    }

    private var cancelHitFrame: CGRect {
        KeyboardLayout.cancelFrame.insetBy(dx: -10, dy: -10)
    }

    /// The suggestion bar above the keys stays SwiftUI's (its buttons), except on the panel.
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        guard bounds.contains(point) else { return false }
        return panelMode || point.y >= keyAreaTop
    }

    // MARK: Touches

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let point = touch.location(in: self)
            let id = ObjectIdentifier(touch)
            if panelMode {
                tracked[id] = .panel(startedOnCancel: cancelHitFrame.contains(point))
                continue
            }
            guard let key = KeyboardLayout.key(at: point, in: frames) else { continue }
            if case .character = key {
                commitHeldCharacters()
                heldCharacters.append(id)
            }
            tracked[id] = .key(KeyTouch(key: key, start: point, last: point))
            delegate?.keyDown(key)
            if key == .globe { delegate?.globeTouch(from: self, with: event) }
            if key == .space { startSpaceHold(id) }
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let id = ObjectIdentifier(touch)
            guard case .key(var item)? = tracked[id] else { continue }
            let point = touch.location(in: self)
            item.last = point
            switch item.key {
            case .globe:
                delegate?.globeTouch(from: self, with: event)
            case .space:
                if !item.cursorMode,
                   hypot(point.x - item.start.x, point.y - item.start.y) > Self.dragToTrackpad {
                    item.cursorMode = true
                    item.anchor = point
                    cancelSpaceHold()
                    delegate?.cursorDragBegan()
                }
                if item.cursorMode {
                    let characters = Int((point.x - item.anchor.x) / Self.cursorStep)
                    if characters != 0 {
                        delegate?.moveCursor(by: characters)
                        item.anchor.x += CGFloat(characters) * Self.cursorStep
                    }
                    let lines = Int((point.y - item.anchor.y) / Self.lineStep)
                    if lines != 0 {
                        delegate?.moveCursor(lines: lines)
                        item.anchor.y += CGFloat(lines) * Self.lineStep
                    }
                }
            case .character:
                if let now = KeyboardLayout.key(at: point, in: frames), now != item.key, case .character = now {
                    let old = item.key
                    item.key = now
                    delegate?.keyMoved(from: old, to: now)
                }
            default:
                break
            }
            tracked[id] = .key(item)
        }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let id = ObjectIdentifier(touch)
            heldCharacters.removeAll { $0 == id }
            guard let tracking = tracked.removeValue(forKey: id) else { continue }
            switch tracking {
            case .panel(let startedOnCancel):
                if startedOnCancel {
                    if cancelHitFrame.contains(touch.location(in: self)) { delegate?.panelCancelTapped() }
                } else {
                    delegate?.panelTapped()
                }
            case .key(let item):
                if item.key == .space { cancelSpaceHold() }
                if item.key == .globe { delegate?.globeTouch(from: self, with: event) }
                if item.key == .space, item.cursorMode {
                    delegate?.keyCancelled(item.key)
                } else {
                    delegate?.keyUp(item.key)
                }
            }
        }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let id = ObjectIdentifier(touch)
            heldCharacters.removeAll { $0 == id }
            guard case .key(let item)? = tracked.removeValue(forKey: id) else { continue }
            if item.key == .space { cancelSpaceHold() }
            if item.key == .globe { delegate?.globeTouch(from: self, with: event) }
            delegate?.keyCancelled(item.key)
        }
    }

    /// Fast typing: the next letter's touch types the ones still held, in order.
    private func commitHeldCharacters() {
        for id in heldCharacters {
            if case .key(let item)? = tracked.removeValue(forKey: id) {
                delegate?.keyUp(item.key)
            }
        }
        heldCharacters.removeAll()
    }

    /// Holding the space bar still: the trackpad starts where the finger is.
    private func startSpaceHold(_ id: ObjectIdentifier) {
        spaceHold?.cancel()
        spaceHold = Task { [weak self] in
            try? await Task.sleep(for: Self.holdToTrackpad)
            guard let self, !Task.isCancelled else { return }
            self.spaceHold = nil
            guard case .key(var item)? = self.tracked[id], item.key == .space, !item.cursorMode else { return }
            item.cursorMode = true
            item.anchor = item.last
            self.tracked[id] = .key(item)
            self.delegate?.cursorDragBegan()
        }
    }

    private func cancelSpaceHold() {
        spaceHold?.cancel()
        spaceHold = nil
    }

    // MARK: VoiceOver

    /// Rebuilds the keys for VoiceOver (also after shift or the return key's label changes).
    func refreshAccessibility() {
        var elements: [UIAccessibilityElement] = []
        if panelMode {
            elements.append(makeElement(name: "Cancel", frame: KeyboardLayout.cancelFrame, traits: .button) { [weak self] in
                self?.delegate?.panelCancelTapped()
            })
            elements.append(makeElement(name: "Finish dictating", frame: bounds, traits: .button) { [weak self] in
                self?.delegate?.panelTapped()
            })
        } else {
            for item in frames {
                let key = item.key
                let name = delegate?.spokenName(for: key) ?? ""
                elements.append(makeElement(name: name, frame: item.frame, traits: .keyboardKey) { [weak self] in
                    guard let delegate = self?.delegate else { return }
                    if key == .globe {
                        delegate.advanceToNextKeyboard()
                    } else {
                        delegate.keyDown(key)
                        delegate.keyUp(key)
                    }
                })
            }
        }
        accessibilityElements = elements
    }

    private func makeElement(
        name: String,
        frame: CGRect,
        traits: UIAccessibilityTraits,
        activate: @escaping @MainActor () -> Void
    ) -> UIAccessibilityElement {
        let element = KeyAccessibilityElement(accessibilityContainer: self)
        element.accessibilityLabel = name
        element.accessibilityTraits = traits
        element.accessibilityFrameInContainerSpace = frame
        element.activate = activate
        return element
    }
}

private final class KeyAccessibilityElement: UIAccessibilityElement {
    var activate: (@MainActor () -> Void)?

    override func accessibilityActivate() -> Bool {
        let action = activate
        MainActor.assumeIsolated { action?() }  // VoiceOver calls this on the main thread
        return true
    }
}
