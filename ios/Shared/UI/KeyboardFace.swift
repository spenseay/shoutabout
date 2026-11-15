import SwiftUI

// What the ShoutAbout keyboard looks like: the iPhone keyboard's layout and colours with the
// mouth where the emoji key sits, the suggestion bar, and the yellow listening panel. Drawing
// only: touches go to the keyboard's KeyTouchSurface (ShoutAboutKeyboard), which uses the same
// `KeyboardLayout` frames. Shared so the app's Debug screenshot scenes can show it too.

/// What the keyboard shows (the keyboard's controller keeps it up to date).
@MainActor
@Observable
final class KeyboardState {
    var hasFullAccess = false
    var showsGlobe = false
    var mouth: KeyboardMouthState = .idle
    var page: KeyboardPage = .letters
    var shift: KeyboardShift = .off
    /// The return key's label: "return", or what the text field asks for ("send", "search").
    var returnLabel = "return"
    /// Blue, like the iPhone keyboard's go / search / send keys.
    var returnIsPrimary = false
    var suggestions: [KeyboardSuggestion] = []
    /// The last dictation typed here, for Insert again. Only in memory.
    var lastText: String?
    /// The suggestion bar offers Insert again (right after a dictation, until you type).
    var showsAgain = false
    /// A short message in the suggestion bar (Full Access, a failed dictation).
    var notice: String?
    var keyFrames: [KeyboardKeyFrame] = []
    var compact = false
    /// The key under a finger: letters show their pop-up, other keys light up.
    var pressed: Set<KeyboardKey> = []
    /// The character key whose pop-up shows (the latest one pressed).
    var popup: KeyboardKey?
    /// Holding the mouth: letting go finishes.
    var holding = false
    /// The text field asked for the dark keyboard.
    var forceDark = false

    var showsPanel: Bool {
        switch mouth {
        case .starting, .listening, .working: return true
        case .idle, .failed: return false
        }
    }

    var barHeight: CGFloat { KeyboardLayout.barHeight(compact: compact) }

    /// What a character key shows (upper case with shift on the letters page).
    func label(for character: String) -> String {
        page == .letters && shift != .off ? character.uppercased() : character
    }
}

/// The suggestion bar's buttons (closures into the controller).
struct KeyboardFaceActions {
    var suggestion: @MainActor (KeyboardSuggestion) -> Void = { _ in }
    var again: @MainActor () -> Void = {}
}

/// The iPhone keyboard's colours.
enum KeyboardPalette {
    static let background = Color(light: Color(hex: 0xD1D3D9), dark: Color(hex: 0x2B2B2D))
    static let key = Color(light: .white, dark: Color(hex: 0x6B6B6E))
    static let functionKey = Color(light: Color(hex: 0xABB0BA), dark: Color(hex: 0x464649))
    static let ink = Color(light: .black, dark: .white)
    static let shadow = Color(light: Color(hex: 0x898A8D), dark: Color.black.opacity(0.55))
    static let divider = Color(light: Color(hex: 0xB4B8C0), dark: Color(hex: 0x4A4A4D))
    static let primary = Color(hex: 0x0A84FF)
    static let mouthShadow = Color(hex: 0xB3A100)
}

struct KeyboardFace: View {
    let state: KeyboardState
    var actions = KeyboardFaceActions()

    var body: some View {
        ZStack(alignment: .topLeading) {
            KeyboardPalette.background
            if state.showsPanel {
                KeyboardListeningPanel(mouth: state.mouth, holding: state.holding)
                    .padding(4)
                    .transition(.opacity)
            } else {
                KeyboardSuggestionBar(state: state, actions: actions)
                    .frame(height: state.barHeight)
                    .frame(maxWidth: .infinity)
                ForEach(Array(state.keyFrames.enumerated()), id: \.offset) { _, item in
                    KeyboardKeyCap(item: item, state: state)
                }
                KeyboardPopup(state: state)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .animation(.easeOut(duration: 0.15), value: state.showsPanel)
        .modifier(ForcedDark(on: state.forceDark))
    }
}

private struct ForcedDark: ViewModifier {
    var on: Bool

    @ViewBuilder
    func body(content: Content) -> some View {
        if on {
            content.environment(\.colorScheme, .dark)
        } else {
            content
        }
    }
}

// MARK: - Keys

private struct KeyboardKeyCap: View {
    let item: KeyboardKeyFrame
    let state: KeyboardState

    var body: some View {
        RoundedRectangle(cornerRadius: 8.5, style: .continuous)
            .fill(fill)
            .shadow(color: shadow, radius: 0, x: 0, y: 1)
            .overlay { label }
            .frame(width: item.frame.width, height: item.frame.height)
            .offset(x: item.frame.minX, y: item.frame.minY)
            .accessibilityHidden(true)  // the touch surface carries the keys' accessibility
    }

    private var isPressed: Bool { state.pressed.contains(item.key) }

    private var isFunction: Bool {
        switch item.key {
        case .character, .space, .mouth: return false
        case .shift: return state.shift == .off
        case .returnKey: return !state.returnIsPrimary
        case .delete, .pageToggle, .symbolsToggle, .globe: return true
        }
    }

    private var fill: Color {
        switch item.key {
        case .mouth:
            return Theme.Palette.shoutYellow
        case .returnKey where state.returnIsPrimary:
            return isPressed ? KeyboardPalette.key : KeyboardPalette.primary
        case .space:
            return isPressed ? KeyboardPalette.functionKey : KeyboardPalette.key
        default:
            if isFunction { return isPressed ? KeyboardPalette.key : KeyboardPalette.functionKey }
            return KeyboardPalette.key
        }
    }

    private var shadow: Color {
        item.key == .mouth ? KeyboardPalette.mouthShadow : KeyboardPalette.shadow
    }

    @ViewBuilder
    private var label: some View {
        let compact = state.compact
        switch item.key {
        case .character(let character):
            Text(state.label(for: character))
                .font(.system(size: compact ? 20 : 23, weight: .regular))
                .foregroundStyle(KeyboardPalette.ink)
                .offset(y: state.page == .letters && state.shift == .off ? -1.5 : 0)
        case .shift:
            Image(systemName: state.shift == .locked ? "capslock.fill" : (state.shift == .on ? "shift.fill" : "shift"))
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(KeyboardPalette.ink)
        case .delete:
            Image(systemName: "delete.left")
                .font(.system(size: 19, weight: .regular))
                .foregroundStyle(KeyboardPalette.ink)
        case .pageToggle:
            Text(state.page == .letters ? "123" : "ABC")
                .font(.system(size: 16))
                .foregroundStyle(KeyboardPalette.ink)
        case .symbolsToggle:
            Text(state.page == .numbers ? "#+=" : "123")
                .font(.system(size: 16))
                .foregroundStyle(KeyboardPalette.ink)
        case .globe:
            Image(systemName: "globe")
                .font(.system(size: 19))
                .foregroundStyle(KeyboardPalette.ink)
        case .mouth:
            // The primary mark, centred on its yellow key.
            let width = min(item.frame.width * 0.62, item.frame.height * 0.58 / Theme.Mouth.openRatio)
            MouthView(ratio: Theme.Mouth.openRatio, tongue: true, style: .standard)
                .frame(width: width, height: width * Theme.Mouth.openRatio)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .space:
            Text("space")
                .font(.system(size: 16))
                .foregroundStyle(KeyboardPalette.ink)
        case .returnKey:
            Text(state.returnLabel)
                .font(.system(size: 16, weight: state.returnIsPrimary ? .semibold : .regular))
                .foregroundStyle(state.returnIsPrimary && !isPressed ? Color.white : KeyboardPalette.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                .padding(.horizontal, 4)
        }
    }
}

/// The iPhone's letter pop-up: a bigger copy of the key above your finger.
private struct KeyboardPopup: View {
    let state: KeyboardState

    var body: some View {
        if !state.compact, case .character(let character)? = state.popup,
           let frame = state.keyFrames.first(where: { $0.key == state.popup })?.frame {
            let width = frame.width + 22
            let height = frame.height + 54
            let x = min(max(2, frame.midX - width / 2), max(2, maxX - width - 2))
            let y = max(0, frame.maxY - height)
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(KeyboardPalette.key)
                .shadow(color: .black.opacity(0.28), radius: 3, x: 0, y: 1)
                .overlay(alignment: .top) {
                    Text(state.label(for: character))
                        .font(.system(size: 36, weight: .regular))
                        .foregroundStyle(KeyboardPalette.ink)
                        .padding(.top, 6)
                }
                .frame(width: width, height: height)
                .offset(x: x, y: y)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    private var maxX: CGFloat {
        state.keyFrames.map(\.hitFrame.maxX).max() ?? 0
    }
}

// MARK: - Suggestion bar

private struct KeyboardSuggestionBar: View {
    let state: KeyboardState
    let actions: KeyboardFaceActions

    var body: some View {
        if let notice = state.notice {
            Text(notice)
                .font(.system(size: 14))
                .foregroundStyle(KeyboardPalette.ink.opacity(0.75))
                .multilineTextAlignment(.center)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
                .padding(.horizontal, 16)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if state.showsAgain, let last = state.lastText {
            KeyboardAgainChip(text: last, action: actions.again)
                .padding(.horizontal, 6)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if !state.suggestions.isEmpty {
            let corrects = state.suggestions.contains { $0.kind == .correction }
            HStack(spacing: 0) {
                ForEach(Array(state.suggestions.enumerated()), id: \.offset) { index, suggestion in
                    if index > 0 {
                        Rectangle()
                            .fill(KeyboardPalette.divider)
                            .frame(width: 1, height: 22)
                    }
                    Button {
                        actions.suggestion(suggestion)
                    } label: {
                        Text(suggestion.kind == .typed && corrects ? "“\(suggestion.text)”" : suggestion.text)
                            .font(.system(size: 16))
                            .foregroundStyle(KeyboardPalette.ink)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                            .padding(.horizontal, 8)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .background {
                                if suggestion.kind == .correction {
                                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                                        .fill(KeyboardPalette.key)
                                        .padding(.vertical, 5)
                                        .padding(.horizontal, 3)
                                }
                            }
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(suggestion.text)
                }
            }
            .padding(.horizontal, 4)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Color.clear
        }
    }
}

/// Type the last dictation again: the start of what you said, and a plain label.
private struct KeyboardAgainChip: View {
    var text: String
    var action: @MainActor () -> Void

    var body: some View {
        Button {
            action()
        } label: {
            HStack(spacing: 10) {
                Text("“\(text)”")
                    .font(.system(size: 15))
                    .foregroundStyle(KeyboardPalette.ink.opacity(0.7))
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text("Insert again")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.Palette.black)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(Theme.Palette.shoutYellow, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            }
            .padding(.leading, 12)
            .padding(.trailing, 5)
            .frame(height: 34)
            .background(KeyboardPalette.key, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .shadow(color: KeyboardPalette.shadow, radius: 0, x: 0, y: 1)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Insert the last dictation again")
    }
}

// MARK: - Listening

/// While you dictate the keys make way: the big mouth, centred, a labeled Cancel, and "tap
/// anywhere to finish" (the whole panel finishes; KeyTouchSurface handles the touches).
private struct KeyboardListeningPanel: View {
    var mouth: KeyboardMouthState
    var holding: Bool

    var body: some View {
        GeometryReader { geo in
            let mouthWidth = min(132, geo.size.width * 0.36, geo.size.height * 0.42 / Theme.Mouth.openRatio)
            ZStack(alignment: .topLeading) {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Theme.Palette.shoutYellow)
                VStack(spacing: 10) {
                    face
                        .frame(width: mouthWidth, height: mouthWidth * Theme.Mouth.openRatio)
                    Text(title)
                        .font(.system(size: 17, weight: .semibold))
                    if let subtitle {
                        Text(subtitle)
                            .font(.system(size: 13))
                            .opacity(0.7)
                    }
                }
                .foregroundStyle(Theme.Palette.black)
                .frame(width: geo.size.width, height: geo.size.height)
                if mouth != .working {
                    KeyboardCancelPill()
                        // The panel is inset 4 from the keyboard; the frame is the keyboard's.
                        .offset(x: KeyboardLayout.cancelFrame.minX - 4, y: KeyboardLayout.cancelFrame.minY - 4)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
    }

    @ViewBuilder
    private var face: some View {
        switch mouth {
        case .working:
            if MotionPolicy.frozen {
                MouthView(ratio: Theme.Mouth.ajarRatio, tongue: true, style: .standard)
            } else {
                TalkingMouthView(style: .standard)
            }
        default:
            KeyboardListeningMouth()
        }
    }

    private var title: String {
        switch mouth {
        case .starting: return "Starting…"
        case .working: return "Writing it down…"
        default: return "Listening"
        }
    }

    private var subtitle: String? {
        switch mouth {
        case .working: return nil
        default: return holding ? "Let go to finish" : "Tap anywhere to finish"
        }
    }
}

private struct KeyboardCancelPill: View {
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "xmark")
                .font(.system(size: 13, weight: .bold))
            Text("Cancel")
                .font(.system(size: 15, weight: .semibold))
        }
        .foregroundStyle(.white)
        .frame(width: KeyboardLayout.cancelFrame.width, height: KeyboardLayout.cancelFrame.height)
        .background(Capsule().fill(Theme.Palette.black))
    }
}

/// Listening: wide open and moving.
private struct KeyboardListeningMouth: View {
    var body: some View {
        if MotionPolicy.frozen {
            MouthView(ratio: Theme.Mouth.openRatio * 0.85, tongue: true, style: .standard)
        } else {
            PhaseAnimator([Theme.Mouth.openRatio, Theme.Mouth.openRatio * 0.62]) { ratio in
                MouthView(ratio: ratio, tongue: true, style: .standard)
            } animation: { _ in
                .easeInOut(duration: 0.45)
            }
        }
    }
}
