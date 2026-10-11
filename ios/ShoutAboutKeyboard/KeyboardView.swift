import SwiftUI
import UIKit

/// What the keyboard shows (the controller keeps it up to date).
@MainActor
@Observable
final class KeyboardState {
    var hasFullAccess = false
    var showsGlobe = false
    var mouth: KeyboardMouthState = .idle
    /// A dictation was just typed: the mouth zips shut (`zipTick` replays it).
    var zipping = false
    var zipTick = 0
    /// The last dictation typed here, for the "again" chip. Only in memory.
    var lastText: String?
}

/// The keys' actions (closures into the controller, which hold it weakly).
struct KeyboardActions {
    var mouth: @MainActor () -> Void
    var again: @MainActor () -> Void
    var space: @MainActor () -> Void
    var newline: @MainActor () -> Void
    var deleteDown: @MainActor () -> Void
    var deleteUp: @MainActor () -> Void
    /// The controller, for the globe key (iOS's own keyboard switcher).
    var inputModeTarget: @MainActor () -> UIInputViewController?
}

/// The big mouth in the middle; underneath, the globe (only when iOS asks for it), space,
/// delete and return. Shout Yellow ground (black in dark mode), black keys.
struct KeyboardView: View {
    let state: KeyboardState
    let actions: KeyboardActions

    var body: some View {
        VStack(spacing: Theme.Spacing.s) {
            ZStack(alignment: .topLeading) {
                KeyboardMouthTile(
                    mouth: state.mouth,
                    zipping: state.zipping,
                    zipTick: state.zipTick,
                    enabled: state.hasFullAccess,
                    action: actions.mouth
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                if state.hasFullAccess, state.lastText != nil {
                    AgainChip(action: actions.again)
                }
            }
            .frame(maxHeight: .infinity)
            if !state.hasFullAccess {
                Text("Allow Full Access to dictate")
                    .font(Theme.Typography.body(13))
                    .foregroundStyle(Theme.Colors.inkMuted)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            HStack(spacing: 6) {
                if state.showsGlobe {
                    KeyCap(systemImage: "globe", label: "Next keyboard", inputModeTarget: actions.inputModeTarget)
                        .frame(width: 50)
                }
                KeyCap(label: "Space", onUp: { inside in
                    if inside { actions.space() }
                })
                .frame(maxWidth: .infinity)
                KeyCap(
                    systemImage: "delete.left",
                    label: "Delete",
                    onDown: { actions.deleteDown() },
                    onUp: { _ in actions.deleteUp() }
                )
                .frame(width: 58)
                KeyCap(systemImage: "return", label: "Return", onUp: { inside in
                    if inside { actions.newline() }
                })
                .frame(width: 74)
            }
            .frame(height: 44)
        }
        .padding(.horizontal, 6)
        .padding(.top, Theme.Spacing.s)
        .padding(.bottom, 6)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.Colors.ground)
    }
}

// MARK: - The mouth

/// The app icon's tile with the mouth: breathing when idle, wide open and moving with a red
/// ring while listening, talking while the words are written, red when it failed, zipping
/// shut when a dictation was typed.
private struct KeyboardMouthTile: View {
    var mouth: KeyboardMouthState
    var zipping: Bool
    var zipTick: Int
    var enabled: Bool
    var action: @MainActor () -> Void

    var body: some View {
        Button {
            action()
        } label: {
            GeometryReader { geo in
                // Room for the listening ring around it.
                let side = max(40, min(geo.size.width, geo.size.height) - 16)
                let radius = side * Theme.Radius.tileFraction
                ZStack {
                    if mouth.isListening {
                        RoundedRectangle(cornerRadius: radius * 1.1, style: .continuous)
                            .stroke(Theme.Palette.tongueRed, lineWidth: 4)
                            .padding(-7)
                    }
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .fill(mouth == .failed ? Theme.Colors.tileError : Theme.Colors.button)
                    face
                        .frame(width: side * Theme.Mouth.tileFraction)
                        .frame(width: side, height: side)
                        .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(Theme.Colors.buttonStroke, lineWidth: 2.5)
                }
                .frame(width: side, height: side)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .buttonStyle(PressStyle())
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .accessibilityLabel(accessibilityLabel)
    }

    @ViewBuilder
    private var face: some View {
        switch mouth {
        case .idle:
            if zipping {
                ZipFinishMouth(style: .standard)
                    .id(zipTick)
            } else {
                BreathingMouth(style: .standard)
            }
        case .starting, .listening:
            ListeningMouth()
        case .working:
            TalkingMouthView(style: .standard)
        case .failed:
            MouthView(ratio: Theme.Mouth.ajarRatio, tongue: false, style: .standard)
        }
    }

    private var accessibilityLabel: String {
        switch mouth {
        case .starting, .listening: return "Stop"
        case .working: return "Writing"
        case .idle, .failed: return "Dictate"
        }
    }
}

/// Listening: wide open and moving (the voice level stays in the app).
private struct ListeningMouth: View {
    var body: some View {
        if MotionPolicy.frozen {
            MouthView(ratio: Theme.Mouth.openRatio * 0.8, tongue: true, style: .standard)
        } else {
            PhaseAnimator([Theme.Mouth.openRatio, Theme.Mouth.openRatio * 0.62]) { ratio in
                MouthView(ratio: ratio, tongue: true, style: .standard)
            } animation: { _ in
                .easeInOut(duration: 0.45)
            }
        }
    }
}

/// Type the last dictation again.
private struct AgainChip: View {
    var action: @MainActor () -> Void

    var body: some View {
        Button {
            action()
        } label: {
            Image(systemName: "arrow.counterclockwise")
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(Theme.Colors.pillOnInk)
                .frame(width: 36, height: 36)
                .background(Circle().fill(Theme.Colors.pillOn))
                .contentShape(Circle())
        }
        .buttonStyle(PressStyle())
        .accessibilityLabel("Type it again")
    }
}

private struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(.spring(duration: 0.2, bounce: 0.5), value: configuration.isPressed)
    }
}

// MARK: - Keys

/// A black key. Touches go through a UIKit control (`KeyTouchArea`): down and up as UIKit
/// reports them (delete repeats while held), and the globe needs the real control and event.
private struct KeyCap: View {
    var systemImage: String?
    var label: String
    var onDown: @MainActor () -> Void = {}
    /// `inside`: the finger lifted on the key (not slid off it).
    var onUp: @MainActor (_ inside: Bool) -> Void = { _ in }
    var inputModeTarget: (@MainActor () -> UIInputViewController?)?
    @State private var pressed = false

    var body: some View {
        RoundedRectangle(cornerRadius: Theme.Radius.chip, style: .continuous)
            .fill(Theme.Colors.pillOn)
            .overlay {
                if let systemImage {
                    Image(systemName: systemImage)
                        .font(.system(size: 18, weight: .bold))
                        .foregroundStyle(Theme.Colors.pillOnInk)
                }
            }
            .opacity(pressed ? 0.65 : 1)
            .overlay {
                KeyTouchArea(
                    inputModeTarget: inputModeTarget,
                    onDown: {
                        pressed = true
                        onDown()
                    },
                    onUp: { inside in
                        pressed = false
                        onUp(inside)
                    }
                )
            }
            .accessibilityElement()
            .accessibilityLabel(label)
            .accessibilityAddTraits(.isButton)
    }
}

/// A clear UIControl over a key.
private struct KeyTouchArea: UIViewRepresentable {
    var inputModeTarget: (@MainActor () -> UIInputViewController?)?
    var onDown: @MainActor () -> Void
    var onUp: @MainActor (_ inside: Bool) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeUIView(context: Context) -> UIControl {
        let control = UIControl()
        control.backgroundColor = .clear
        if let target = inputModeTarget?() {
            // iOS's keyboard switcher: tap for the next keyboard, hold for the list. UIControl
            // doesn't retain its targets.
            control.addTarget(
                target,
                action: #selector(UIInputViewController.handleInputModeList(from:with:)),
                for: .allTouchEvents
            )
        }
        control.addTarget(context.coordinator, action: #selector(Coordinator.touchDown), for: .touchDown)
        control.addTarget(context.coordinator, action: #selector(Coordinator.touchUpInside), for: .touchUpInside)
        control.addTarget(context.coordinator, action: #selector(Coordinator.touchUpOutside), for: [.touchUpOutside, .touchCancel])
        return control
    }

    func updateUIView(_ control: UIControl, context: Context) {
        context.coordinator.parent = self
    }

    @MainActor
    final class Coordinator: NSObject {
        var parent: KeyTouchArea

        init(_ parent: KeyTouchArea) {
            self.parent = parent
            super.init()
        }

        @objc func touchDown() {
            parent.onDown()
        }

        @objc func touchUpInside() {
            parent.onUp(true)
        }

        @objc func touchUpOutside() {
            parent.onUp(false)
        }
    }
}
