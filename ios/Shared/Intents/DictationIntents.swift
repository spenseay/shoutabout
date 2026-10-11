import AppIntents
import WidgetKit

// Intents compiled into BOTH the app and the widget extension (Control Center controls and the
// Live Activity's Stop button refer to them). `SHOUTABOUT_APP` is only defined for the app
// target, so the app-only hooks (`DictationLauncher`) are left out of the extension build.
//
// The recording intents adopt AudioRecordingIntent + LiveActivityIntent, so iOS runs them in the
// APP's process (launched in the background if needed) even when Control Center or the Live
// Activity triggers them, and lets them start the microphone and a Live Activity without
// opening the app. iOS keeps a background recording going only while a Live Activity is on
// screen, so the app starts one before the microphone.

/// Action Button / Control Center / Siri / Shortcuts: start recording without opening the app
/// (a mouth appears in the Dynamic Island), or, while recording, stop and send.
struct DictateIntent: AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "Dictate"
    static let description = IntentDescription(
        "Starts recording without opening ShoutAbout. Run it again to stop and send."
    )
    /// Background first; the app comes forward only when it has to (Live Activities off, or
    /// the microphone permission hasn't been asked yet).
    static let supportedModes: IntentModes = [.background, .foreground(.dynamic)]
    /// From a locked iPhone, iOS asks for Face ID first: nobody can dictate into your PC with
    /// your phone without unlocking it.
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        #if SHOUTABOUT_APP
        switch await DictationLauncher.toggleFromSystem() {
        case .started, .finished, .busy:
            break
        case .needsForeground(let reason):
            guard systemContext.currentMode.canContinueInForeground else {
                throw DictationIntentError(reason)
            }
            try await continueInForeground(nil, alwaysConfirm: false)
            DictationLauncher.startWhenActive()
        case .failed(let reason):
            throw DictationIntentError(reason)
        }
        #else
        PendingDictation.request()
        #endif
        return .result()
    }
}

/// The Live Activity's Stop button: stop recording and send.
struct StopDictationIntent: AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "Stop Dictating"
    static let description = IntentDescription("Stops the recording and sends it.")
    static let supportedModes: IntentModes = .background
    static let isDiscoverable = false

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        #if SHOUTABOUT_APP
        await DictationLauncher.stopFromSystem()
        #endif
        return .result()
    }
}

/// Foreground fallback: open ShoutAbout and start recording (the pre-Live-Activity behaviour).
/// Running it again while recording stops and sends.
struct DictateInAppIntent: AppIntent {
    static let title: LocalizedStringResource = "Dictate in App"
    static let description = IntentDescription(
        "Opens ShoutAbout and starts recording. Run it again to stop and send."
    )
    static let supportedModes: IntentModes = .foreground(.immediate)

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        #if SHOUTABOUT_APP
        DictationLauncher.handleInAppIntent()
        #else
        PendingDictation.request()
        #endif
        return .result()
    }
}

/// A reason a recording intent couldn't run, shown by the system.
struct DictationIntentError: Error, CustomLocalizedStringResourceConvertible {
    let message: String

    init(_ message: String) {
        self.message = message
    }

    var localizedStringResource: LocalizedStringResource {
        LocalizedStringResource(stringLiteral: message)
    }
}

/// Flip between Computer (paste on the PC) and Phone (copy on the iPhone).
struct ToggleTargetIntent: AppIntent {
    static let title: LocalizedStringResource = "Toggle Dictation Target"
    static let description = IntentDescription(
        "Switches ShoutAbout between typing on your computer and copying on this phone."
    )

    init() {}

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let settings = SharedSettings()
        let newTarget = settings.target.toggled
        settings.target = newTarget
        await TargetSync.didChange()
        return .result(dialog: "Dictation goes to your \(newTarget.label) now.")
    }
}

/// Set the target explicitly (Shortcuts automations).
struct SetTargetIntent: AppIntent {
    static let title: LocalizedStringResource = "Set Dictation Target"
    static let description = IntentDescription("Chooses where ShoutAbout delivers your dictation.")

    @Parameter(title: "Target", default: .pc)
    var target: DictationTargetEntity

    init() {}

    func perform() async throws -> some IntentResult {
        SharedSettings().target = target.dictationTarget
        await TargetSync.didChange()
        return .result()
    }
}

/// Backs the Control Center toggle: on = Phone, off = Computer.
struct SetPhoneModeIntent: SetValueIntent {
    static let title: LocalizedStringResource = "Phone Mode"
    static let description = IntentDescription("On: copy dictation on this phone. Off: type it on your computer.")

    @Parameter(title: "Phone mode")
    var value: Bool

    init() {}

    func perform() async throws -> some IntentResult {
        SharedSettings().target = value ? .phone : .pc
        await TargetSync.didChange()
        return .result()
    }
}

/// `DictationTarget` as an App Intents enum (for the Shortcuts parameter picker).
enum DictationTargetEntity: String, AppEnum {
    case pc
    case phone

    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Dictation Target"
    static let caseDisplayRepresentations: [DictationTargetEntity: DisplayRepresentation] = [
        .pc: DisplayRepresentation(title: "Computer", subtitle: "Type into the focused window on your PC"),
        .phone: DisplayRepresentation(title: "Phone", subtitle: "Copy to this phone's clipboard"),
    ]

    var dictationTarget: DictationTarget { self == .pc ? .pc : .phone }
}

/// Tells controls, widgets and (in the app) the UI that the target changed.
enum TargetSync {
    @MainActor
    static func didChange() {
        ControlCenter.shared.reloadControls(ofKind: WidgetKinds.targetControl)
        WidgetCenter.shared.reloadTimelines(ofKind: WidgetKinds.lockScreen)
        #if SHOUTABOUT_APP
        DictationLauncher.targetChangedElsewhere()
        #endif
    }
}
