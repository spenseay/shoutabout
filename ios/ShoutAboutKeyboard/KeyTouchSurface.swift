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
    /// Dragging along the space bar moves the cursor.
    func moveCursor(by characters: Int)
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
/// the iPhone keyboard, lets thumbs overlap (a new letter commits the one still held), moves the
/// cursor when you drag along the space bar, and on the listening panel turns any tap into
/// "finish" (Cancel apart). A touch that began on a key stays that key's until it lifts, even
/// if the panel appears meanwhile (holding the mouth to talk).
final class KeyTouchSurface: UIView {
    weak var delegate: KeyTouchSurfaceDelegate?

    private(set) var frames: [KeyboardKeyFrame] = []
    private(set) var keyAreaTop: CGFloat = 0
    private(set) var panelMode = false

    private enum Tracked {
        case key(KeyboardKey, start: CGPoint, cursorMode: Bool, anchorX: CGFloat)
        case panel(startedOnCancel: Bool)
    }

    private var tracked: [ObjectIdentifier: Tracked] = [:]
    /// Character touches still down, oldest first.
    private var heldCharacters: [ObjectIdentifier] = []

    /// Points of drag along the space bar per character the cursor moves.
    private static let cursorStep: CGFloat = 9

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
            tracked[id] = .key(key, start: point, cursorMode: false, anchorX: point.x)
            delegate?.keyDown(key)
            if key == .globe { delegate?.globeTouch(from: self, with: event) }
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let id = ObjectIdentifier(touch)
            guard case .key(let key, let start, var cursorMode, var anchorX)? = tracked[id] else { continue }
            let point = touch.location(in: self)
            switch key {
            case .globe:
                delegate?.globeTouch(from: self, with: event)
            case .space:
                if !cursorMode, abs(point.x - start.x) > 14 {
                    cursorMode = true
                    anchorX = point.x
                }
                if cursorMode {
                    let steps = Int((point.x - anchorX) / Self.cursorStep)
                    if steps != 0 {
                        delegate?.moveCursor(by: steps)
                        anchorX += CGFloat(steps) * Self.cursorStep
                    }
                }
                tracked[id] = .key(key, start: start, cursorMode: cursorMode, anchorX: anchorX)
            case .character:
                if let now = KeyboardLayout.key(at: point, in: frames), now != key, case .character = now {
                    tracked[id] = .key(now, start: start, cursorMode: false, anchorX: anchorX)
                    delegate?.keyMoved(from: key, to: now)
                }
            default:
                break
            }
        }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let id = ObjectIdentifier(touch)
            heldCharacters.removeAll { $0 == id }
            guard let item = tracked.removeValue(forKey: id) else { continue }
            switch item {
            case .panel(let startedOnCancel):
                if startedOnCancel {
                    if cancelHitFrame.contains(touch.location(in: self)) { delegate?.panelCancelTapped() }
                } else {
                    delegate?.panelTapped()
                }
            case .key(let key, _, let cursorMode, _):
                if key == .globe { delegate?.globeTouch(from: self, with: event) }
                if key == .space, cursorMode {
                    delegate?.keyCancelled(key)
                } else {
                    delegate?.keyUp(key)
                }
            }
        }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let id = ObjectIdentifier(touch)
            heldCharacters.removeAll { $0 == id }
            guard case .key(let key, _, _, _)? = tracked.removeValue(forKey: id) else { continue }
            if key == .globe { delegate?.globeTouch(from: self, with: event) }
            delegate?.keyCancelled(key)
        }
    }

    /// Fast typing: the next letter's touch types the ones still held, in order.
    private func commitHeldCharacters() {
        for id in heldCharacters {
            if case .key(let key, _, _, _)? = tracked.removeValue(forKey: id) {
                delegate?.keyUp(key)
            }
        }
        heldCharacters.removeAll()
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
