import SwiftUI

/// The ShoutAbout keyboard opened the app to start listening. Going back (the system's link at
/// the top left) keeps it listening in the background; the keyboard's mouth, or this one, stops.
struct KeyboardListeningView: View {
    var body: some View {
        GeometryReader { geo in
            let tile = max(150, min(geo.size.width - 96, geo.size.height * 0.40, 280))
            VStack(spacing: Theme.Spacing.xl) {
                Spacer(minLength: 0)
                MouthButton()
                    .frame(width: tile, height: tile)
                VStack(spacing: Theme.Spacing.s) {
                    Text("Listening")
                        .shoutHeadline(36)
                    Text("Go back to keep typing.")
                        .font(Theme.Typography.body(17))
                        .foregroundStyle(Theme.Colors.inkMuted)
                }
                .multilineTextAlignment(.center)
                Spacer(minLength: 0)
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .background(Theme.Colors.ground.ignoresSafeArea())
    }
}
