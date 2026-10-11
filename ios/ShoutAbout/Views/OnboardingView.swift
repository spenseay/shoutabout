import SwiftUI
import UIKit

enum OnboardingStep: Hashable, Sendable {
    case choose
    case phone
    case computer
    case actionButton
}

/// First launch, at most three short screens: where should your words go -> (microphone |
/// find your computer) -> one press to talk (the Action Button). Everything can be skipped and
/// redone from Settings.
struct OnboardingView: View {
    @Environment(AppModel.self) private var model
    @State private var step: OnboardingStep = OnboardingView.initialStep
    @State private var chosenTarget: DictationTarget = .phone

    var body: some View {
        ZStack {
            Theme.Colors.ground.ignoresSafeArea()
            Group {
                switch step {
                case .choose:
                    ChooseStep(
                        onPhone: { go(.phone) },
                        onComputer: { go(.computer) }
                    )
                case .phone:
                    PhoneStep(onNext: {
                        chosenTarget = .phone
                        go(.actionButton)
                    })
                case .computer:
                    ConnectComputerView(
                        onConnected: {
                            chosenTarget = .pc
                            go(.actionButton)
                        },
                        onSkip: { go(.actionButton) }
                    )
                case .actionButton:
                    ActionButtonStep(onDone: { model.finishOnboarding(target: chosenTarget) })
                }
            }
            .transition(.asymmetric(
                insertion: .move(edge: .trailing).combined(with: .opacity),
                removal: .move(edge: .leading).combined(with: .opacity)
            ))
        }
        .overlay(alignment: .topLeading) {
            if step == .phone || step == .computer {
                CircleIconButton(systemImage: "chevron.left", label: "Back") { go(.choose) }
                    .padding(.leading, Theme.Spacing.l)
                    .padding(.top, Theme.Spacing.s)
            }
        }
        .overlay(alignment: .topTrailing) {
            if step == .choose {
                Button("Skip") { model.finishOnboarding(target: .phone) }
                    .font(Theme.Typography.bold(16))
                    .foregroundStyle(Theme.Colors.ink)
                    .padding(.horizontal, Theme.Spacing.l)
                    .padding(.top, Theme.Spacing.m)
            }
        }
    }

    private func go(_ next: OnboardingStep) {
        withAnimation(.spring(duration: 0.4)) { step = next }
    }

    private nonisolated static var initialStep: OnboardingStep {
#if DEBUG
        return ScreenshotScene.current?.onboardingStep ?? .choose
#else
        return .choose
#endif
    }
}

// MARK: - Step 1: where should your words go?

private struct ChooseStep: View {
    var onPhone: () -> Void
    var onComputer: () -> Void

    var body: some View {
        GeometryReader { geo in
            ScrollView {
                content
                    .frame(maxWidth: .infinity, minHeight: geo.size.height)
            }
            .scrollIndicators(.hidden)
        }
    }

    /// Centred vertically on tall phones, scrolls on small ones.
    private var content: some View {
        VStack(spacing: 0) {
            Spacer(minLength: Theme.Spacing.huge)
            VStack(spacing: 0) {
                LockupView(size: 50)
                Text("Where should your words go?")
                    .shoutHeadline(40)
                    .multilineTextAlignment(.center)
                    .padding(.top, Theme.Spacing.xl)
                VStack(spacing: Theme.Spacing.m) {
                    ChoiceCard(
                        systemImage: "iphone",
                        title: "This iPhone",
                        detail: "Copied to your clipboard.",
                        action: onPhone
                    )
                    .accessibilityIdentifier("choosePhone")
                    ChoiceCard(
                        systemImage: "desktopcomputer",
                        title: "My computer",
                        detail: "Typed into your PC.",
                        action: onComputer
                    )
                    .accessibilityIdentifier("chooseComputer")
                }
                .padding(.top, Theme.Spacing.xl)
                Label("Your voice stays on your devices.", systemImage: "lock.fill")
                    .font(Theme.Typography.body(13))
                    .foregroundStyle(Theme.Colors.inkMuted)
                    .padding(.top, Theme.Spacing.l)
            }
            .padding(.horizontal, Theme.Spacing.xl)
            Spacer(minLength: Theme.Spacing.xl)
        }
    }
}

private struct ChoiceCard: View {
    var systemImage: String
    var title: String
    var detail: String
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: Theme.Spacing.l) {
                Image(systemName: systemImage)
                    .font(.system(size: 24, weight: .bold))
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .frame(width: 56, height: 56)
                    .background(Circle().fill(Theme.Palette.black))
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(Theme.Typography.bold(20))
                        .foregroundStyle(Theme.Colors.cardInk)
                    Text(detail)
                        .font(Theme.Typography.body(14))
                        .foregroundStyle(Theme.Colors.cardInkMuted)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(Theme.Colors.cardInk)
            }
            .padding(Theme.Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous).fill(Theme.Colors.card))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                    .stroke(Theme.Colors.ink, lineWidth: Theme.Size.borderWidth)
            )
            .contentShape(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        }
        .buttonStyle(SquishButtonStyle())
    }
}

// MARK: - Step 2a: this iPhone (microphone + speech)

private struct PhoneStep: View {
    var onNext: () -> Void
    @State private var asking = false

    var body: some View {
        VStack(spacing: Theme.Spacing.l) {
            Spacer(minLength: Theme.Spacing.huge)
            MouthTile(size: 150)
            Text("Let it hear you")
                .shoutHeadline(42)
                .multilineTextAlignment(.center)
            Text("Only while you dictate.")
                .font(Theme.Typography.body(17))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
            PrimaryButton(title: "Allow microphone", busy: asking) {
                asking = true
                Task {
                    _ = await AudioRecorder.requestPermission()
                    _ = await OfflineTranscriber.requestSpeechPermission()
                    asking = false
                    onNext()
                }
            }
            Button("Not now", action: onNext)
                .font(Theme.Typography.bold(16))
                .foregroundStyle(Theme.Colors.ink)
                .padding(.bottom, Theme.Spacing.m)
        }
        .padding(.horizontal, Theme.Spacing.xl)
    }
}

// MARK: - Step 3: the Action Button

private struct ActionButtonStep: View {
    var onDone: () -> Void
    @State private var micGranted = AudioRecorder.permission == .granted
    @State private var asking = false

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(spacing: Theme.Spacing.xl) {
                    ActionButtonGuide()
                    ControlCenterGuide()
                }
                .padding(.horizontal, Theme.Spacing.xl)
                .padding(.top, Theme.Spacing.xl)
                .padding(.bottom, Theme.Spacing.l)
            }
            .scrollIndicators(.hidden)
            VStack(spacing: Theme.Spacing.s) {
                if !micGranted {
                    PrimaryButton(title: "Allow microphone", style: .secondary, busy: asking) {
                        asking = true
                        Task {
                            micGranted = await AudioRecorder.requestPermission()
                            _ = await OfflineTranscriber.requestSpeechPermission()
                            asking = false
                        }
                    }
                }
                PrimaryButton(title: "Done", action: onDone)
                    .accessibilityIdentifier("onboardingDone")
            }
            .padding(.horizontal, Theme.Spacing.xl)
            .padding(.bottom, Theme.Spacing.m)
        }
    }
}

/// "One press to talk": how to put Dictate on the Action Button (end of the setup, and in
/// Settings). iOS doesn't let an app assign the Action Button or add a Control Center control
/// by itself, so: one line, the exact steps and a button into Settings (no steps on a phone
/// without one). The Control Center card (`ControlCenterGuide`) goes with it.
struct ActionButtonGuide: View {
    /// nil when the model can't be told (then the steps show, with the model note).
    private var hasActionButton: Bool? { DeviceFeatures.thisPhoneHasActionButton }

    var body: some View {
        VStack(spacing: Theme.Spacing.l) {
            ActionButtonIllustration()
                .frame(height: 210)
            Text("One press to talk")
                .shoutHeadline(40)
                .multilineTextAlignment(.center)
            Text("Press once to talk, again to send.")
                .font(Theme.Typography.body(16))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if hasActionButton != false {
                GuideCard(title: "Action Button", steps: [
                    "Open Settings › Action Button.",
                    "Swipe to Controls, tap Choose a Control.",
                    "Pick ShoutAbout › Dictate.",
                ])
                OpenSettingsButton()
                if hasActionButton == nil {
                    Text("iPhone 15 Pro or later.")
                        .font(Theme.Typography.body(14))
                        .foregroundStyle(Theme.Colors.inkMuted)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// Dictate in Control Center (any iPhone): the steps.
struct ControlCenterGuide: View {
    var body: some View {
        GuideCard(title: "Control Center", steps: [
            "Swipe down from the top-right corner.",
            "Tap + at the top left, then Add a Control.",
            "Search for ShoutAbout and tap Dictate.",
        ])
    }
}

/// Opens ShoutAbout's own page in the Settings app: `openSettingsURLString` is the only
/// Settings link an app may use (private "App-prefs:" links get an app rejected), so the
/// line under it says where to go from there.
struct OpenSettingsButton: View {
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: Theme.Spacing.xs) {
            PrimaryButton(title: "Open Settings", style: .secondary) {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    openURL(url)
                }
            }
            Text("Then go back and tap Action Button.")
                .font(Theme.Typography.body(13))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// A numbered how-to on a paper card.
struct GuideCard: View {
    var title: String
    var steps: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            Text(title)
                .font(Theme.Typography.bold(18))
                .foregroundStyle(Theme.Colors.cardInk)
            ForEach(steps.indices, id: \.self) { index in
                GuideRow(number: index + 1, text: steps[index])
            }
        }
        .padding(Theme.Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous).fill(Theme.Colors.card))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                .stroke(Theme.Colors.ink, lineWidth: Theme.Size.borderWidth)
        )
    }
}

private struct GuideRow: View {
    var number: Int
    var text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            Text("\(number)")
                .font(Theme.Typography.display(20))
                .foregroundStyle(Theme.Palette.shoutYellow)
                .frame(width: 30, height: 30)
                .background(Circle().fill(Theme.Palette.black))
            Text(text)
                .font(Theme.Typography.bold(16))
                .foregroundStyle(Theme.Colors.cardInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// An iPhone outline with the Action Button lit up red and the little mouth in the island.
private struct ActionButtonIllustration: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 30, style: .continuous)
                .fill(Theme.Colors.card)
                .frame(width: 118, height: 206)
            RoundedRectangle(cornerRadius: 30, style: .continuous)
                .stroke(Theme.Colors.ink, lineWidth: 4)
                .frame(width: 118, height: 206)
            Capsule()
                .fill(Theme.Palette.black)
                .frame(width: 70, height: 22)
                .overlay {
                    HStack(spacing: 0) {
                        // The island's little yellow tile with the mouth (like the Live Activity).
                        ZStack {
                            RoundedRectangle(cornerRadius: 16 * Theme.Radius.tileFraction, style: .continuous)
                                .fill(Theme.Palette.shoutYellow)
                            MouthView(ratio: Theme.Mouth.openRatio * 0.85, tongue: true, style: .standard)
                                .frame(width: 16 * Theme.Mouth.tileFraction)
                        }
                        .frame(width: 16, height: 16)
                        Spacer(minLength: 0)
                        Text("0:03")
                            .font(.system(size: 8, weight: .bold).monospacedDigit())
                            .foregroundStyle(Theme.Palette.shoutYellow)
                    }
                    .padding(.horizontal, 7)
                }
                .offset(y: -80)
            Capsule()
                .fill(Theme.Palette.tongueRed)
                .frame(width: 8, height: 26)
                .offset(x: -62, y: -50)
            Image(systemName: "hand.point.right.fill")
                .font(.system(size: 34, weight: .bold))
                .foregroundStyle(Theme.Colors.ink)
                .offset(x: -100, y: -50)
            MouthTile(size: 66, open: true)
                .offset(y: 20)
        }
        .frame(maxWidth: .infinity)
        .accessibilityHidden(true)
    }
}

// MARK: - Shared pieces

/// The app-icon tile: yellow (red when something failed), the primary mark inside, a thin ink
/// edge. The mouth breathes (ajar) unless `open` (wide open, tongue out, still).
struct MouthTile: View {
    var size: CGFloat
    var failed = false
    var open = false

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: size * Theme.Radius.tileFraction, style: .continuous)
                .fill(failed ? Theme.Colors.tileError : Theme.Colors.button)
            Group {
                if failed {
                    MouthView(ratio: Theme.Mouth.ajarRatio, tongue: false, style: .standard)
                } else if open {
                    MouthView(ratio: Theme.Mouth.openRatio, tongue: true, style: .standard)
                } else if MotionPolicy.frozen {
                    MouthView(ratio: Theme.Mouth.ajarRatio * 1.15, tongue: false, style: .standard)
                } else {
                    PhaseAnimator([Theme.Mouth.ajarRatio, Theme.Mouth.ajarRatio * 1.25]) { ratio in
                        MouthView(ratio: ratio, tongue: false, style: .standard)
                    } animation: { _ in
                        .easeInOut(duration: 1.4)
                    }
                }
            }
            .frame(width: size * Theme.Mouth.tileFraction)
            RoundedRectangle(cornerRadius: size * Theme.Radius.tileFraction, style: .continuous)
                .strokeBorder(Theme.Colors.buttonStroke, lineWidth: size >= 100 ? Theme.Size.borderWidth : 2)
        }
        .frame(width: size, height: size)
    }
}

/// Big capsule button on the ground.
struct PrimaryButton: View {
    enum Style {
        case primary
        case secondary
    }

    var title: String
    var style: Style = .primary
    var busy = false
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: Theme.Spacing.s) {
                if busy {
                    ProgressView()
                        .tint(style == .primary ? Theme.Colors.primaryButtonInk : Theme.Colors.ink)
                }
                Text(title)
                    .font(Theme.Typography.bold(18))
            }
            .frame(maxWidth: .infinity)
            .frame(height: 56)
            .foregroundStyle(style == .primary ? Theme.Colors.primaryButtonInk : Theme.Colors.ink)
            .background(
                Capsule().fill(style == .primary ? Theme.Colors.primaryButton : Color.clear)
            )
            .overlay(
                Capsule().stroke(Theme.Colors.ink, lineWidth: style == .primary ? 0 : Theme.Size.borderWidth)
            )
            .contentShape(Capsule())
        }
        .buttonStyle(SquishButtonStyle())
        .disabled(busy)
    }
}
