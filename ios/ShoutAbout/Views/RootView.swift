import SwiftUI

/// First launch: the short setup. Afterwards: the one main screen.
struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            if model.onboardingDone {
                MainView()
                    .transition(.opacity)
            } else {
                OnboardingView()
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.3), value: model.onboardingDone)
        .tint(Theme.Colors.tint)
#if DEBUG
        .preferredColorScheme(ScreenshotScene.forcedColorScheme)
#endif
    }
}

/// "SH[mouth]UT / ABOUT": the brand lockup in Anton, with a live mouth as the O.
struct LockupView: View {
    var size: CGFloat = Theme.Size.lockupFont
    /// Mouth height / width (OPEN by default).
    var mouthRatio: CGFloat = Theme.Mouth.openRatio
    var tongue: Bool = true
    /// saTalk loop instead of a fixed ratio.
    var talking: Bool = false
    var color: Color = Theme.Colors.ink

    var body: some View {
        // The primary mark in both modes (no lip ring).
        let style: Theme.MouthStyle = .standard
        VStack(spacing: -size * Theme.Typography.displayLineOverlap) {
            HStack(alignment: .center, spacing: size * 0.02) {
                Text("SH")
                Group {
                    if talking {
                        TalkingMouthView(style: style, speed: 1.4)
                    } else {
                        MouthView(ratio: mouthRatio, tongue: tongue, style: style)
                    }
                }
                .frame(width: size * 0.78)
                .offset(y: size * 0.02)
                Text("UT")
            }
            Text("ABOUT")
        }
        .shoutHeadline(size, color: color)
        .lineLimit(1)
        .minimumScaleFactor(0.5)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("ShoutAbout")
    }
}
