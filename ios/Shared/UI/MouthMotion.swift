import SwiftUI

// The mouth's idle breathing and its "done" zip, shared by the app's big button and the
// ShoutAbout keyboard.

/// Idle mouth: ajar, slowly breathing (it "rests open when idle").
struct BreathingMouth: View {
    var style: Theme.MouthStyle

    var body: some View {
        if MotionPolicy.frozen {
            MouthView(ratio: Theme.Mouth.ajarRatio * 1.15, tongue: false, style: style)
        } else {
            PhaseAnimator([Theme.Mouth.ajarRatio, Theme.Mouth.ajarRatio * 1.25]) { ratio in
                MouthView(ratio: ratio, tongue: false, style: style)
            } animation: { _ in
                .easeInOut(duration: 1.4)
            }
        }
    }
}

/// Done, the words landed: the mouth zips shut (the desktop's "Paddle pull" finish, ~0.7 s),
/// flattens away and the idle mouth pops back in, breathing. It plays once, from the moment
/// this view appears (the moment the phase turned done). Reduce Motion: the sealed zipper
/// shows and fades into the idle mouth.
struct ZipFinishMouth: View {
    var style: Theme.MouthStyle
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var startedAt = Date()
    @State private var finished = MotionPolicy.frozen

    init(style: Theme.MouthStyle) {
        self.style = style
    }

    var body: some View {
        Group {
            if finished {
                BreathingMouth(style: style)
            } else {
                TimelineView(.animation) { context in
                    let t = context.date.timeIntervalSince(startedAt)
                    ZipFinishFrame(
                        pose: ZipTimeline.pose(at: t, reduceMotion: reduceMotion),
                        zipScale: Theme.Mouth.zipTileFraction / Theme.Mouth.tileFraction,
                        style: style
                    )
                }
            }
        }
        .task {
            // Then the plain idle mouth takes over (and the per-frame timeline stops).
            let remaining = ZipTimeline.length(reduceMotion: reduceMotion) - Date().timeIntervalSince(startedAt)
            if remaining > 0 {
                try? await Task.sleep(for: .seconds(remaining))
            }
            guard !Task.isCancelled else { return }
            finished = true
        }
    }
}
