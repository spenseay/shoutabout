import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

/// The mouth pop-up: while you dictate from the Action Button (or anywhere), a little mouth
/// sits in the Dynamic Island and opens with your voice; it chews while the PC works, then
/// zips shut ("Pasted · on your PC"). The Lock Screen shows the same as a banner.
///
/// Live Activities can't run their own animations: the app sends a new state when the voice
/// level changes (quantised + throttled), a few saTalk frames a second while sending and a few
/// stills along the zip when it lands, and the mouth animates between them.
struct DictationLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: DictationActivityAttributes.self) { context in
            DictationLockScreenView(state: context.state)
                .activityBackgroundTint(Theme.Palette.black)
                .activitySystemActionForegroundColor(Theme.Palette.shoutYellow)
                .widgetURL(LiveActivityLinks.open)
        } dynamicIsland: { context in
            let state = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    IslandMouth(state: state, size: 54)
                        .padding(.leading, 6)
                        .padding(.top, 6)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ExpandedTrailing(state: state)
                        .padding(.trailing, 6)
                        .padding(.top, 4)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    ExpandedBottom(state: state)
                        .padding(.horizontal, 8)
                        .padding(.bottom, 4)
                }
            } compactLeading: {
                IslandMouth(state: state, size: 26)
                    .padding(.leading, 2)
                    .accessibilityLabel(state.summary)
            } compactTrailing: {
                CompactTrailing(state: state)
            } minimal: {
                IslandMouth(state: state, size: 22)
                    .accessibilityLabel(state.summary)
            }
            .keylineTint(Theme.Palette.shoutYellow)
            .widgetURL(LiveActivityLinks.open)
        }
    }
}

enum LiveActivityLinks {
    static let open = URL(string: "shoutabout://open")
}

// MARK: - Pieces

/// The app icon in miniature: a yellow tile (red when it failed) with the primary mark, the
/// mouth driven by the activity state. Delivered, the mouth zips shut (the app sends a few
/// stills along the zip, then the sealed zipper stays until the activity ends).
struct IslandMouth: View {
    let state: DictationActivityState
    /// Side of the square tile, in points.
    var size: CGFloat

    var body: some View {
        let ratio = Self.ratio(for: state)
        ZStack {
            RoundedRectangle(cornerRadius: size * Theme.Radius.tileFraction, style: .continuous)
                .fill(state.phase == .failed ? Theme.Palette.tongueRed : Theme.Palette.shoutYellow)
            if state.zips {
                let progress = state.zipProgress
                ZipMouthView(pose: ZipTimeline.still(progress: progress), style: .standard)
                    .frame(width: size * Theme.Mouth.tileFraction)
                    .animation(.easeInOut(duration: 0.25), value: progress)
            } else {
                MouthView(ratio: ratio, tongue: state.phase != .failed, style: .standard)
                    .frame(width: size * Theme.Mouth.tileFraction)
                    .animation(.easeOut(duration: 0.22), value: ratio)
            }
        }
        .frame(width: size, height: size)
        // The zip's pull may swing out past the mouth: it stays inside the tile.
        .clipShape(RoundedRectangle(cornerRadius: size * Theme.Radius.tileFraction, style: .continuous))
    }

    static func ratio(for state: DictationActivityState) -> CGFloat {
        switch state.phase {
        case .listening:
            let open = Theme.Motion.micMinOpen + (Theme.Motion.micMaxOpen - Theme.Motion.micMinOpen) * CGFloat(state.openness)
            return Theme.Mouth.openRatio * open
        case .sending:
            let index = (abs(state.frame) % 9) + 1
            return Theme.Mouth.openRatio * Theme.Motion.talkScale(index)
        case .done:
            return Theme.Mouth.openRatio
        case .failed:
            return Theme.Mouth.ajarRatio
        }
    }
}

/// Elapsed recording time; iOS updates it by itself, no activity updates needed.
struct ElapsedText: View {
    let startedAt: Date

    var body: some View {
        Text(timerInterval: startedAt...startedAt.addingTimeInterval(600), countsDown: false, showsHours: false)
            .monospacedDigit()
    }
}

/// Compact trailing: the elapsed time while listening, then where it went.
struct CompactTrailing: View {
    let state: DictationActivityState

    var body: some View {
        Group {
            if state.phase == .listening {
                ElapsedText(startedAt: state.startedAt)
                    .font(.system(size: 14, weight: .semibold))
                    .multilineTextAlignment(.trailing)
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .frame(width: 42, alignment: .trailing)
            } else if state.phase == .done {
                Text(state.badge)
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .lineLimit(1)
                    .fixedSize()
                    .accessibilityLabel(state.summary)
            } else {
                StatusGlyph(state: state)
                    .font(.system(size: 15, weight: .semibold))
            }
        }
        .padding(.trailing, 2)
    }
}

struct StatusGlyph: View {
    let state: DictationActivityState

    var body: some View {
        switch state.phase {
        case .listening, .sending:
            Image(systemName: state.target.systemImage)
                .foregroundStyle(Theme.Palette.shoutYellow)
                .accessibilityLabel(state.summary)
        case .done:
            // Where the words went (no check mark: the zipped mouth says it's done).
            Image(systemName: state.doneSymbol)
                .foregroundStyle(Theme.Palette.shoutYellow)
                .accessibilityLabel(state.summary)
        case .failed:
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(Theme.Palette.tongueRed)
                .accessibilityLabel(state.summary)
        }
    }
}

/// Red stop button (the Live Activity's only control).
struct StopButton: View {
    var size: CGFloat = 44

    var body: some View {
        Button(intent: StopDictationIntent()) {
            ZStack {
                Circle().fill(Theme.Palette.tongueRed)
                RoundedRectangle(cornerRadius: size * 0.09, style: .continuous)
                    .fill(Theme.Palette.teeth)
                    .frame(width: size * 0.34, height: size * 0.34)
            }
            .frame(width: size, height: size)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Stop and send")
    }
}

struct ExpandedTrailing: View {
    let state: DictationActivityState

    var body: some View {
        if state.phase == .listening {
            StopButton(size: 48)
        } else {
            StatusGlyph(state: state)
                .font(.system(size: 30, weight: .semibold))
                .frame(width: 48, height: 48)
        }
    }
}

struct ExpandedBottom: View {
    let state: DictationActivityState

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(state.headline.uppercased())
                .font(Theme.Typography.display(26))
                .foregroundStyle(Theme.Palette.shoutYellow)
                .lineLimit(1)
            Text(state.phase == .listening || state.phase == .sending ? "· \(state.detail)" : state.detail)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Theme.Palette.teeth.opacity(0.85))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 4)
            if state.phase == .listening {
                ElapsedText(startedAt: state.startedAt)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .frame(width: 48, alignment: .trailing)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// Lock Screen (and the banner on iPhones without a Dynamic Island).
struct DictationLockScreenView: View {
    let state: DictationActivityState

    var body: some View {
        HStack(spacing: 14) {
            IslandMouth(state: state, size: 60)
            VStack(alignment: .leading, spacing: 2) {
                Text(state.headline.uppercased())
                    .font(Theme.Typography.display(28))
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .lineLimit(1)
                Text(state.detail)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.Palette.teeth.opacity(0.85))
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            if state.phase == .listening {
                VStack(alignment: .trailing, spacing: 6) {
                    StopButton(size: 44)
                    ElapsedText(startedAt: state.startedAt)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.Palette.shoutYellow)
                        .frame(width: 44, alignment: .trailing)
                }
            } else {
                StatusGlyph(state: state)
                    .font(.system(size: 28, weight: .semibold))
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
    }
}
