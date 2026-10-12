import Foundation

// The ShoutAbout keyboard and the app. A keyboard extension can't use the microphone, so the app
// records and the keyboard types the result. They share the App Group's defaults (the keyboard
// reaches them only with Full Access) and nudge each other with Darwin notifications, which
// carry no data (`KeyboardSignals`):
//
//   keyboard -> app   start, stop   a tap on the keyboard's mouth
//   app -> keyboard   state         `KeyboardAppState` changed (listening, working, ...)
//                     result        a `KeyboardHandoff` is waiting to be typed
//
// With a keyboard session open (the app keeps its microphone input running in the background),
// a tap starts a dictation right away; without one the keyboard opens shoutabout://keyboard and
// the app opens a session and starts listening. Every decision is a pure function here
// (unit-tested in ShoutAboutTests); the keyboard and the app only act on them.

/// The Darwin notification names.
enum KeyboardSignal: String, CaseIterable, Sendable {
    case start = "fyi.shoutabout.keyboard.start"
    case stop = "fyi.shoutabout.keyboard.stop"
    case state = "fyi.shoutabout.keyboard.state"
    case result = "fyi.shoutabout.keyboard.result"
}

/// What the app tells the keyboard about itself. The app writes it again every
/// `refreshInterval` while it runs (the heartbeat), so the state of an app that was stopped or
/// crashed expires by itself after `validity`.
struct KeyboardAppState: Codable, Equatable, Sendable {
    enum Phase: String, Codable, Sendable {
        case idle
        case listening
        case working
        case failed
    }

    var phase: Phase
    /// When `phase` began.
    var since: Date
    /// A keyboard session is open: the app keeps its microphone input running, so a tap on
    /// the keyboard's mouth starts a dictation without leaving the app you're typing in.
    var sessionOpen: Bool
    /// The heartbeat: the app vouches for all of this until then.
    var aliveUntil: Date

    static let refreshInterval: TimeInterval = 10
    static let validity: TimeInterval = 25
    /// A failed dictation shows (red tile) this long on the keyboard.
    static let failedShowsFor: TimeInterval = 4

    /// The state as the app writes it at `now`.
    static func heartbeat(phase: Phase, since: Date, sessionOpen: Bool, now: Date) -> KeyboardAppState {
        KeyboardAppState(phase: phase, since: since, sessionOpen: sessionOpen, aliveUntil: now.addingTimeInterval(validity))
    }

    func isAlive(at now: Date) -> Bool {
        now <= aliveUntil
    }

    /// The phase, or idle once the heartbeat stopped.
    func phase(at now: Date) -> Phase {
        isAlive(at: now) ? phase : .idle
    }

    /// A keyboard session is open and the app is alive, so it hears a `start` signal.
    func sessionIsAlive(at now: Date) -> Bool {
        sessionOpen && isAlive(at: now)
    }
}

/// What the keyboard's mouth shows.
enum KeyboardMouthState: Equatable, Sendable {
    case idle
    /// Asked the app to start; it hasn't said "listening" yet.
    case starting
    case listening
    case working
    case failed

    static func make(app: KeyboardAppState?, startPending: Bool, now: Date) -> KeyboardMouthState {
        switch app?.phase(at: now) ?? .idle {
        case .listening:
            return .listening
        case .working:
            return .working
        case .failed:
            if startPending { return .starting }
            if let app, now.timeIntervalSince(app.since) < KeyboardAppState.failedShowsFor { return .failed }
            return .idle
        case .idle:
            return startPending ? .starting : .idle
        }
    }

    /// The microphone is (about to be) on.
    var isListening: Bool { self == .starting || self == .listening }
}

/// What a tap on the keyboard's mouth does.
enum KeyboardMouthAction: Equatable, Sendable {
    /// A keyboard session is open: a Darwin `start`, the app starts listening at once.
    case signalStart
    /// No session: open the app (shoutabout://keyboard), which opens one and starts listening.
    case openApp
    /// Listening (whatever started it), or a start not answered yet: stop and send.
    case signalStop
    /// Working on the last one, or no Full Access (the keyboard can't reach the App Group).
    case ignore

    static func decide(hasFullAccess: Bool, app: KeyboardAppState?, startPending: Bool, now: Date) -> KeyboardMouthAction {
        guard hasFullAccess else { return .ignore }
        switch app?.phase(at: now) ?? .idle {
        case .listening:
            return .signalStop
        case .working:
            return .ignore
        case .idle, .failed:
            if startPending { return .signalStop }
            return app?.sessionIsAlive(at: now) == true ? .signalStart : .openApp
        }
    }
}

/// After a `start` signal the app says "listening" within a moment. When it doesn't (it was
/// stopped since its last heartbeat), the keyboard opens it instead.
enum KeyboardStartWait {
    static let timeout: TimeInterval = 2

    static func shouldOpenApp(pendingSince: Date, app: KeyboardAppState?, now: Date) -> Bool {
        guard now.timeIntervalSince(pendingSince) >= timeout else { return false }
        switch app?.phase(at: now) ?? .idle {
        case .idle, .failed: return true
        case .listening, .working: return false
        }
    }
}

/// A finished phone dictation waiting for the keyboard. Only the latest one is ever kept, and
/// only until it's typed or `maxAge` passed.
struct KeyboardHandoff: Codable, Equatable, Sendable {
    var id: String
    var text: String
    var createdAt: Date

    /// Typed by itself only this soon after it finished (dictate first, then tap into a field).
    static let insertWindow: TimeInterval = 60
    /// Removed after this, typed or not.
    static let maxAge: TimeInterval = 3 * 60

    init(id: String = UUID().uuidString, text: String, createdAt: Date = Date()) {
        self.id = id
        self.text = text
        self.createdAt = createdAt
    }

    /// The keyboard types it on its own (it's on screen, or comes up within the window).
    func canAutoInsert(at now: Date) -> Bool {
        let age = now.timeIntervalSince(createdAt)
        return !text.isEmpty && age >= -60 && age <= Self.insertWindow
    }

    /// Too old to keep (or from the future: the clock changed).
    func isExpired(at now: Date) -> Bool {
        let age = now.timeIntervalSince(createdAt)
        return age > Self.maxAge || age < -60
    }

    /// Which finished dictations go to the keyboard: phone ones with words, once the keyboard
    /// is set up (nothing waits in the App Group for a keyboard that's never used). Never one
    /// sent to the computer (it was typed there).
    static func shouldPublish(toPhone: Bool, text: String, keyboardSetUp: Bool) -> Bool {
        toPhone && keyboardSetUp && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

/// "This keyboard types handoff X", stored as `id claimant` (ids are UUIDs: no spaces).
struct KeyboardClaim: Equatable, Sendable {
    var id: String
    var claimant: String

    init(id: String, claimant: String) {
        self.id = id
        self.claimant = claimant
    }

    init?(stored: String) {
        let parts = stored.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }
        id = String(parts[0])
        claimant = String(parts[1])
    }

    var stored: String { id + " " + claimant }
}

/// Spacing when typing a dictation at the cursor.
enum KeyboardText {
    /// `text` as typed after `before` (the text before the cursor): with a space in front
    /// when it would otherwise run into the previous word or sentence.
    static func insertion(_ text: String, after before: String?) -> String {
        guard let last = before?.last, let first = text.first else { return text }
        let endsWord = last.isLetter || last.isNumber || ".,!?;:)]}”’".contains(last)
        let startsWord = first.isLetter || first.isNumber || "(“".contains(first)
        return endsWord && startsWord ? " " + text : text
    }
}

/// The app's keyboard session ends by itself after `idleTimeout` without a dictation (the
/// microphone indicator then goes off).
enum KeyboardSessionPolicy {
    static let idleTimeout: TimeInterval = 5 * 60

    static func shouldClose(lastUse: Date, now: Date, dictating: Bool) -> Bool {
        !dictating && now.timeIntervalSince(lastUse) >= idleTimeout
    }
}

/// The keyboard link's records in the App Group.
struct KeyboardLinkStore {
    enum Key {
        static let appState = "keyboardAppState"
        static let handoff = "keyboardHandoff"
        static let claim = "keyboardHandoffClaim"
        static let fullAccessSeenAt = "keyboardFullAccessSeenAt"
    }

    let defaults: UserDefaults

    init(defaults: UserDefaults = AppGroup.defaults) {
        self.defaults = defaults
    }

    var appState: KeyboardAppState? {
        get { decode(KeyboardAppState.self, Key.appState) }
        nonmutating set { encode(newValue, Key.appState) }
    }

    /// The waiting dictation, unless there's none or it expired (then it's removed).
    func handoff(now: Date = Date()) -> KeyboardHandoff? {
        guard let handoff = decode(KeyboardHandoff.self, Key.handoff) else { return nil }
        guard !handoff.isExpired(at: now) else {
            defaults.removeObject(forKey: Key.handoff)
            return nil
        }
        return handoff
    }

    /// The latest dictation replaces any earlier one.
    func publish(_ handoff: KeyboardHandoff) {
        encode(handoff, Key.handoff)
    }

    /// Removes the waiting dictation; with an `id`, only if it's still that one.
    func removeHandoff(id: String? = nil) {
        if let id, let current = decode(KeyboardHandoff.self, Key.handoff), current.id != id { return }
        defaults.removeObject(forKey: Key.handoff)
    }

    /// Compare-and-set: claims `handoff` for `claimant` unless a keyboard (this one included)
    /// already claimed it. Then, after a short settle, `holdsClaim` says whether the claim
    /// stood: two keyboards claiming at the same moment both write, the last write wins, and
    /// both read the same winner.
    func claim(_ handoff: KeyboardHandoff, by claimant: String) -> Bool {
        if let existing = defaults.string(forKey: Key.claim).flatMap(KeyboardClaim.init(stored:)),
           existing.id == handoff.id {
            return false
        }
        defaults.set(KeyboardClaim(id: handoff.id, claimant: claimant).stored, forKey: Key.claim)
        return true
    }

    func holdsClaim(_ handoff: KeyboardHandoff, by claimant: String) -> Bool {
        defaults.string(forKey: Key.claim) == KeyboardClaim(id: handoff.id, claimant: claimant).stored
    }

    /// Gives a claim back (the keyboard went away before typing it), so the next one can.
    func releaseClaim(_ handoff: KeyboardHandoff, by claimant: String) {
        guard holdsClaim(handoff, by: claimant) else { return }
        defaults.removeObject(forKey: Key.claim)
    }

    /// When the keyboard last came up with Full Access (Settings shows "Keyboard: On").
    var fullAccessSeenAt: Date? {
        get { defaults.object(forKey: Key.fullAccessSeenAt) as? Date }
        nonmutating set {
            if let newValue {
                defaults.set(newValue, forKey: Key.fullAccessSeenAt)
            } else {
                defaults.removeObject(forKey: Key.fullAccessSeenAt)
            }
        }
    }

    var keyboardIsSetUp: Bool { fullAccessSeenAt != nil }

    private func decode<T: Decodable>(_ type: T.Type, _ key: String) -> T? {
        guard let data = defaults.data(forKey: key) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(T.self, from: data)
    }

    private func encode<T: Encodable>(_ value: T?, _ key: String) {
        guard let value else {
            defaults.removeObject(forKey: key)
            return
        }
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        if let data = try? encoder.encode(value) {
            defaults.set(data, forKey: key)
        }
    }
}
