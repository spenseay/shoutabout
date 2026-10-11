import Foundation

/// Where the text goes: pasted into the focused window on the paired computer, or copied on
/// this iPhone. The raw values match the hub's `target` values and the stored settings.
enum DictationTarget: String, Codable, Sendable, CaseIterable, Identifiable {
    case pc
    case phone

    var id: String { rawValue }

    var toggled: DictationTarget { self == .pc ? .phone : .pc }

    /// Short label for buttons and controls.
    var label: String { self == .pc ? "Computer" : "Phone" }

    var systemImage: String { self == .pc ? "desktopcomputer" : "iphone" }
}

/// How a dictation reached text. `lan` and `tunnel` mirror the hub's `route` field; `offline`
/// means it never left the phone (on-device speech recognition).
enum Route: String, Codable, Sendable, CaseIterable {
    case lan
    case tunnel
    case offline

    var label: String {
        switch self {
        case .lan: return "Wi-Fi"
        case .tunnel: return "Tunnel"
        case .offline: return "iPhone"
        }
    }
}

/// Which on-device engine is used. Users never have to touch this: `automatic` picks the best
/// one the iPhone supports (the picker lives under Settings › Advanced).
enum OfflineEnginePreference: String, Codable, Sendable, CaseIterable, Identifiable {
    /// SpeechTranscriber (iOS 26's new long-form model) when the device supports it, otherwise
    /// DictationTranscriber, otherwise the legacy SFSpeechRecognizer.
    case automatic
    /// DictationTranscriber: the only iOS 26 module that honours custom vocabulary
    /// (AnalysisContext contextual strings).
    case vocabulary
    /// SFSpeechRecognizer with requiresOnDeviceRecognition + contextualStrings.
    case legacy

    var id: String { rawValue }

    var label: String {
        switch self {
        case .automatic: return "Automatic"
        case .vocabulary: return "Custom vocabulary"
        case .legacy: return "Legacy recognizer"
        }
    }
}

/// One finished (or failed) dictation, kept locally for History.
struct HistoryItem: Codable, Identifiable, Equatable, Sendable {
    var id: UUID = UUID()
    var date: Date = Date()
    var text: String
    var rawText: String?
    var target: DictationTarget
    var route: Route
    /// True when the computer pasted it.
    var pasted: Bool = false
    /// A short note for the user ("Your PC isn't reachable.").
    var warning: String?
    var cleanup: String?
    var timings: TranscribeResponse.Timings?
    /// Length of the recording in seconds.
    var audioSeconds: Double?
    /// Phone-measured time from "stop" to the answer, including the upload.
    var roundTripMs: Int?
    /// On-device engine name when this iPhone wrote the text.
    var engine: String?
    /// Set when the dictation failed; `text` is empty then.
    var error: String?

    var succeeded: Bool { error == nil }
}
