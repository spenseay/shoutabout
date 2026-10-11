@preconcurrency import AVFoundation
import Foundation
@preconcurrency import Speech

/// On-device transcription for offline mode (and as the fallback when the PC is unreachable).
///
/// iOS 26 Speech framework:
/// - `SpeechTranscriber` (new long-form model) when the device supports it. Best accuracy, but
///   per Apple's docs it does NOT use contextual strings.
/// - `DictationTranscriber` honours `AnalysisContext.contextualStrings` (up to 100 short
///   phrases): used whenever there are custom words (Automatic), for the "Custom vocabulary"
///   preference, and when SpeechTranscriber is unavailable.
/// - `SFSpeechRecognizer` with `requiresOnDeviceRecognition` + `contextualStrings` is the last
///   resort (and the "Legacy" preference).
/// Whatever wrote it, `VocabFixer` then puts the custom words in their own spelling (also when
/// they only sound alike: "Sophia" -> "Sofia").
/// Each model's assets download once (needs a network the first time); `prepare()` does that
/// ahead of time from Settings.
enum OfflineTranscriber {
    struct Result: Sendable {
        var text: String
        var engine: String
    }

    enum OfflineError: Error, LocalizedError {
        case unsupportedLocale
        case notAuthorized
        case unavailable(String)

        var errorDescription: String? {
            switch self {
            case .unsupportedLocale: return "On-device speech recognition doesn't support this language."
            case .notAuthorized: return "Speech recognition is turned off for ShoutAbout in Settings."
            case .unavailable(let why): return why
            }
        }
    }

    static var locale: Locale { Locale.current }

    static func transcribe(fileURL: URL, vocab: [String], preference: OfflineEnginePreference) async throws -> Result {
        let terms = Array(vocab.prefix(100))
        switch preference {
        case .automatic:
            // With custom words, the model that takes them as hints goes first; SpeechTranscriber
            // would ignore them. If it can't run here (language, or its model can't download
            // right now), the long-form model below still writes it down.
            var triedDictation = false
            if !terms.isEmpty {
                triedDictation = true
                do {
                    let text = try await dictationTranscriber(fileURL: fileURL, vocab: terms)
                    return Result(text: VocabFixer.apply(text, terms: vocab), engine: "DictationTranscriber")
                } catch {
                    try Task.checkCancellation()
                }
            }
            if SpeechTranscriber.isAvailable {
                do {
                    let text = try await speechTranscriber(fileURL: fileURL, vocab: terms)
                    return Result(text: VocabFixer.apply(text, terms: vocab), engine: "SpeechTranscriber")
                } catch OfflineError.unsupportedLocale {
                    // fall through to DictationTranscriber / the legacy recognizer
                }
            }
            if triedDictation {
                let text = try await legacy(fileURL: fileURL, vocab: terms)
                return Result(text: VocabFixer.apply(text, terms: vocab), engine: "SFSpeechRecognizer")
            }
            return try await vocabularyFirst(fileURL: fileURL, terms: terms, vocab: vocab)
        case .vocabulary:
            return try await vocabularyFirst(fileURL: fileURL, terms: terms, vocab: vocab)
        case .legacy:
            let text = try await legacy(fileURL: fileURL, vocab: terms)
            return Result(text: VocabFixer.apply(text, terms: vocab), engine: "SFSpeechRecognizer")
        }
    }

    private static func vocabularyFirst(fileURL: URL, terms: [String], vocab: [String]) async throws -> Result {
        do {
            let text = try await dictationTranscriber(fileURL: fileURL, vocab: terms)
            return Result(text: VocabFixer.apply(text, terms: vocab), engine: "DictationTranscriber")
        } catch OfflineError.unsupportedLocale {
            let text = try await legacy(fileURL: fileURL, vocab: terms)
            return Result(text: VocabFixer.apply(text, terms: vocab), engine: "SFSpeechRecognizer")
        }
    }

    // MARK: SpeechAnalyzer modules (iOS 26)

    private static func speechTranscriber(fileURL: URL, vocab: [String]) async throws -> String {
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw OfflineError.unsupportedLocale
        }
        let transcriber = SpeechTranscriber(locale: supported, preset: .transcription)
        try await installAssets(for: [transcriber])
        let results = transcriber.results
        let collector = Task { () async throws -> String in
            var parts: [String] = []
            for try await result in results where result.isFinal {
                parts.append(String(result.text.characters))
            }
            return OfflineTranscriber.join(parts)
        }
        try await analyze(fileURL: fileURL, modules: [transcriber], vocab: vocab)
        return try await collector.value
    }

    private static func dictationTranscriber(fileURL: URL, vocab: [String]) async throws -> String {
        guard let supported = await DictationTranscriber.supportedLocale(equivalentTo: locale) else {
            throw OfflineError.unsupportedLocale
        }
        let transcriber = DictationTranscriber(locale: supported, preset: .longDictation)
        try await installAssets(for: [transcriber])
        let results = transcriber.results
        let collector = Task { () async throws -> String in
            var parts: [String] = []
            for try await result in results where result.isFinal {
                parts.append(String(result.text.characters))
            }
            return OfflineTranscriber.join(parts)
        }
        try await analyze(fileURL: fileURL, modules: [transcriber], vocab: vocab)
        return try await collector.value
    }

    /// Runs one analyzer over the whole file, with the hub vocabulary as contextual strings.
    private static func analyze(fileURL: URL, modules: [any SpeechModule], vocab: [String]) async throws {
        let analyzer = SpeechAnalyzer(modules: modules, options: nil)
        if !vocab.isEmpty {
            let context = AnalysisContext()
            context.contextualStrings[.general] = vocab
            try await analyzer.setContext(context)
        }
        let file = try AVAudioFile(forReading: fileURL)
        if let lastSample = try await analyzer.analyzeSequence(from: file) {
            try await analyzer.finalizeAndFinish(through: lastSample)
        } else {
            await analyzer.cancelAndFinishNow()
        }
    }

    private static func installAssets(for modules: [any SpeechModule]) async throws {
        if let request = try await AssetInventory.assetInstallationRequest(supporting: modules) {
            try await request.downloadAndInstall()
        }
    }

    private static func join(_ parts: [String]) -> String {
        parts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    // MARK: Preparation (Settings → "Download on-device model")

    /// Downloads the models offline mode will need, so it works without a network later.
    static func prepare(preference: OfflineEnginePreference) async throws -> String {
        switch preference {
        case .automatic, .vocabulary:
            var ready: [String] = []
            if preference == .automatic, SpeechTranscriber.isAvailable,
               let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) {
                try await installAssets(for: [SpeechTranscriber(locale: supported, preset: .transcription)])
                ready.append("SpeechTranscriber")
            }
            if let supported = await DictationTranscriber.supportedLocale(equivalentTo: locale) {
                try await installAssets(for: [DictationTranscriber(locale: supported, preset: .longDictation)])
                ready.append("DictationTranscriber")
            }
            guard !ready.isEmpty else { throw OfflineError.unsupportedLocale }
            return "Ready: " + ready.joined(separator: " + ")
        case .legacy:
            guard await authorizeLegacy() else { throw OfflineError.notAuthorized }
            guard let recognizer = SFSpeechRecognizer(locale: locale), recognizer.supportsOnDeviceRecognition else {
                throw OfflineError.unsupportedLocale
            }
            return recognizer.isAvailable ? "Ready: SFSpeechRecognizer (on-device)" : "Recognizer busy; try again"
        }
    }

    /// Human-readable status of the preferred engine's assets (with custom words, Automatic
    /// uses DictationTranscriber, see `transcribe`).
    static func status(preference: OfflineEnginePreference, hasVocabulary: Bool = false) async -> String {
        switch preference {
        case .automatic where SpeechTranscriber.isAvailable && !hasVocabulary:
            guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
                return "Language not supported on this iPhone"
            }
            let status = await AssetInventory.status(forModules: [SpeechTranscriber(locale: supported, preset: .transcription)])
            return describe(status)
        case .automatic, .vocabulary:
            guard let supported = await DictationTranscriber.supportedLocale(equivalentTo: locale) else {
                return "Language not supported on this iPhone"
            }
            let status = await AssetInventory.status(forModules: [DictationTranscriber(locale: supported, preset: .longDictation)])
            return describe(status)
        case .legacy:
            let recognizer = SFSpeechRecognizer(locale: locale)
            return recognizer?.supportsOnDeviceRecognition == true ? "On-device recognition available" : "Not available on this iPhone"
        }
    }

    private static func describe(_ status: AssetInventory.Status) -> String {
        switch status {
        case .installed: return "Model installed"
        case .downloading: return "Model downloading…"
        case .supported: return "Model not downloaded yet"
        case .unsupported: return "Not supported on this iPhone"
        @unknown default: return "Unknown"
        }
    }

    // MARK: SFSpeechRecognizer (legacy)

    /// Asks for speech recognition once, up front (onboarding), so the legacy fallback engine
    /// works later even when a dictation runs in the background, where iOS can't ask.
    static func requestSpeechPermission() async -> Bool {
        await authorizeLegacy()
    }

    private static func authorizeLegacy() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized:
            return true
        case .notDetermined:
            let status = await withCheckedContinuation { (continuation: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
                SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0) }
            }
            return status == .authorized
        default:
            return false
        }
    }

    private static func legacy(fileURL: URL, vocab: [String]) async throws -> String {
        guard await authorizeLegacy() else { throw OfflineError.notAuthorized }
        guard let recognizer = SFSpeechRecognizer(locale: locale) else { throw OfflineError.unsupportedLocale }
        guard recognizer.supportsOnDeviceRecognition else {
            throw OfflineError.unavailable("This iPhone can't recognise speech on-device for this language.")
        }
        let request = SFSpeechURLRecognitionRequest(url: fileURL)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        request.addsPunctuation = true
        request.contextualStrings = vocab
        let once = ResumeOnce()
        let text: String = try await withCheckedThrowingContinuation { continuation in
            _ = recognizer.recognitionTask(with: request) { result, error in
                if let error {
                    if once.claim() { continuation.resume(throwing: error) }
                    return
                }
                guard let result, result.isFinal else { return }
                let best = result.bestTranscription.formattedString
                if once.claim() { continuation.resume(returning: best) }
            }
        }
        withExtendedLifetime(recognizer) {}
        return text
    }
}

/// `OnDeviceTranscribing` for the router: the engine picked automatically (or the Advanced
/// preference), with the PC's vocabulary as hints.
struct OnDeviceEngine: OnDeviceTranscribing {
    var vocab: [String]
    var preference: OfflineEnginePreference

    func transcribe(_ audioURL: URL) async throws -> OnDeviceResult {
        let result = try await OfflineTranscriber.transcribe(fileURL: audioURL, vocab: vocab, preference: preference)
        return OnDeviceResult(text: result.text, engine: result.engine)
    }
}

/// Guards a continuation against being resumed twice by a chatty callback.
final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if done { return false }
        done = true
        return true
    }
}
