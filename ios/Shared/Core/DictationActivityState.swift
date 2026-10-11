import Foundation

/// The dynamic content of the dictation Live Activity (the little mouth in the Dynamic Island
/// and on the Lock Screen). It is the ActivityKit `ContentState`, so it must stay small
/// (ActivityKit caps it at 4 KB) and is pure data: the widget extension draws it.
struct DictationActivityState: Codable, Hashable, Sendable {
    enum Phase: String, Codable, Hashable, Sendable {
        case listening
        case sending
        case done
        case failed
    }

    /// How a finished dictation ended (the done phase).
    enum Outcome: String, Codable, Hashable, Sendable {
        case pasted
        case copied
        case nothing
    }

    var phase: Phase
    var target: DictationTarget
    /// The paired PC's name ("to DESKTOP-ABC"); nil means "Computer".
    var computerName: String?
    /// When recording started (the elapsed-time text counts from it by itself).
    var startedAt: Date
    /// Voice level, quantised (0...levelSteps) so updates stay rare.
    var level: Int = 0
    /// saTalk keyframe while sending (the app advances it a few times a second).
    var frame: Int = 0
    /// The result line or the failure reason, short.
    var message: String?
    /// Set in the done phase.
    var outcome: Outcome?
    /// The done mouth zipping shut (`zips`), while the app plays it as a few stills: 0 = the
    /// slider has just popped in at the left, 1 = sealed. nil = sealed (and what a payload from
    /// before the zip decodes to).
    var zip: Double?
    /// Done, but the text isn't on the clipboard yet: iOS keeps apps in the background away
    /// from it. A tap on the activity opens the app, which copies it (see `PendingCopy`).
    /// nil means false.
    var tapToCopy: Bool?

    static let levelSteps = 4

    /// Mic level 0...1 -> 0...levelSteps.
    static func levelStep(for level: Double) -> Int {
        let clamped = min(max(level.isFinite ? level : 0, 0), 1)
        return Int((clamped * Double(levelSteps)).rounded())
    }

    /// How open the mouth is while listening, 0 (quiet) ... 1 (loud).
    var openness: Double { Double(min(max(level, 0), Self.levelSteps)) / Double(Self.levelSteps) }

    /// Where the words go, for "Listening · to Computer".
    var destination: String {
        target == .pc ? (computerName.nonBlank ?? "Computer") : "Phone"
    }

    /// Big word in the expanded island and on the Lock Screen.
    var headline: String {
        switch phase {
        case .listening: return "Listening"
        case .sending: return target == .pc ? "Sending" : "Writing"
        case .done:
            switch outcome {
            case .pasted: return "Pasted!"
            case .copied: return tapToCopy == true ? "Ready!" : "Copied!"
            case .nothing: return "Nothing heard"
            case nil: return "Done"
            }
        case .failed: return "Didn't land"
        }
    }

    /// The compact island's right side once it's done: "Pasted" / "Copied" (no check mark:
    /// the zipped mouth beside it says it's done), or "Copy ›" while a tap still has to copy it.
    var badge: String {
        switch outcome {
        case .pasted: return "Pasted"
        case .copied: return tapToCopy == true ? "Copy ›" : "Copied"
        case .nothing: return "Nothing"
        case nil: return "Done"
        }
    }

    /// A delivered dictation (pasted or copied) zips the mouth shut; "nothing heard" doesn't.
    var zips: Bool {
        phase == .done && (outcome == .pasted || outcome == .copied)
    }

    /// How far the zip has run, 0...1 (1 = sealed).
    var zipProgress: Double {
        guard let zip, zip.isFinite else { return 1 }
        return min(max(zip, 0), 1)
    }

    /// The glyph beside the done mouth: where the words went.
    var doneSymbol: String {
        switch outcome {
        case .pasted: return "desktopcomputer"
        case .copied: return "doc.on.clipboard"
        case .nothing: return "mic.slash"
        case nil: return target.systemImage
        }
    }

    /// The line under it.
    var detail: String {
        switch phase {
        case .listening: return "to \(destination)"
        case .sending: return target == .pc ? "to \(destination)…" : "for this iPhone…"
        case .done, .failed: return message ?? ""
        }
    }

    /// "Listening · to Computer" (accessibility and the expanded island).
    var summary: String {
        detail.isEmpty ? headline : "\(headline) · \(detail)"
    }

    /// How a delivered dictation ended.
    static func outcome(pasted: Bool, text: String) -> Outcome {
        if text.isEmpty { return .nothing }
        return pasted ? .pasted : .copied
    }

    /// The line under "Pasted!" / "Copied!" / "Ready!" (`tapToCopy`: not on the clipboard yet).
    static func doneDetail(_ outcome: Outcome, notice: String?, computerName: String?, tapToCopy: Bool = false) -> String {
        switch outcome {
        case .pasted:
            return "on \(computerName.nonBlank ?? "your PC")"
        case .copied:
            let away = notice == DictationNotice.away
            if tapToCopy { return away ? "PC away · tap here to copy it" : "Tap here to copy it" }
            return away ? "PC away, so it's on your clipboard" : "on your clipboard"
        case .nothing:
            return "Try again, a little louder"
        }
    }

    /// A failure reason short enough for the island: the first sentence, at most ~40 chars.
    static func shortReason(_ message: String) -> String {
        let firstSentence = message.split(separator: ".", maxSplits: 1).first.map(String.init) ?? message
        let trimmed = firstSentence.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > 40 else { return trimmed }
        return String(trimmed.prefix(39)) + "…"
    }
}

/// Throttles voice-level updates to the Live Activity: only when the quantised level changes,
/// and at most every `minInterval`. ActivityKit updates cost energy and iOS may drop a flood.
struct ActivityLevelThrottle: Sendable {
    var minInterval: TimeInterval = 0.25
    private(set) var lastSent: Date?
    private(set) var lastStep: Int?

    init(minInterval: TimeInterval = 0.25) {
        self.minInterval = minInterval
    }

    mutating func shouldSend(step: Int, at now: Date) -> Bool {
        if step == lastStep { return false }
        if let lastSent, now.timeIntervalSince(lastSent) < minInterval { return false }
        lastSent = now
        lastStep = step
        return true
    }

    mutating func reset() {
        lastSent = nil
        lastStep = nil
    }
}
