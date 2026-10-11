import AppIntents
import SwiftUI

@main
struct ShoutAboutApp: App {
    @State private var model = AppModel.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .onOpenURL { model.handle(url: $0) }
        }
        .onChange(of: scenePhase) { _, newPhase in
            model.scenePhaseChanged(newPhase)
        }
    }
}

/// Siri phrases and the app's actions in Shortcuts / Spotlight / the Action Button picker.
struct ShoutAboutShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: DictateIntent(),
            phrases: [
                "Dictate with \(.applicationName)",
                "Start \(.applicationName)",
                "\(.applicationName) dictation",
            ],
            shortTitle: "Dictate",
            systemImageName: "mouth.fill"
        )
        AppShortcut(
            intent: DictateInAppIntent(),
            phrases: [
                "Open \(.applicationName) and dictate",
            ],
            shortTitle: "Dictate in App",
            systemImageName: "mic.fill"
        )
        AppShortcut(
            intent: ToggleTargetIntent(),
            phrases: [
                "Toggle \(.applicationName) target",
                "Switch \(.applicationName) target",
            ],
            shortTitle: "Computer / Phone",
            systemImageName: "arrow.left.arrow.right"
        )
    }

    static var shortcutTileColor: ShortcutTileColor { .yellow }
}
