import Foundation

// `AppGroup` (the shared defaults) is in AppGroup.swift.

/// Typed access to the settings shared through the App Group. Secrets are NOT here; they live
/// in the keychain (`KeychainStore`).
struct SharedSettings {
    enum Key {
        static let target = "target"
        static let lanURL = "lanURL"
        static let publicURL = "publicURL"
        static let homeSSID = "homeSSID"
        static let preferLAN = "preferLAN"
        static let forceOffline = "forceOffline"
        static let offlineFallback = "offlineFallback"
        static let copyInPCMode = "copyInPCMode"
        static let offlineEngine = "offlineEngine"
        static let vocabTerms = "vocabTerms"
        static let vocabVersion = "vocabVersion"
        static let vocabFetchedAt = "vocabFetchedAt"
        static let pendingDictationAt = "pendingDictationAt"
        static let pairedComputer = "pairedComputer"
        static let deviceID = "deviceID"
        static let hubInfo = "hubInfo"
        static let onboardingDone = "onboardingDone"
        static let useTunnel = "useTunnel"
        static let pendingCopy = "pendingCopy"
    }

    let defaults: UserDefaults

    init(defaults: UserDefaults = AppGroup.defaults) {
        self.defaults = defaults
    }

    var target: DictationTarget {
        get { defaults.string(forKey: Key.target).flatMap(DictationTarget.init(rawValue:)) ?? .pc }
        nonmutating set { defaults.set(newValue.rawValue, forKey: Key.target) }
    }

    var configuration: HubConfiguration {
        get {
            HubConfiguration(
                lanURL: defaults.string(forKey: Key.lanURL).flatMap { URL(string: $0) },
                publicURL: defaults.string(forKey: Key.publicURL).flatMap { URL(string: $0) },
                homeSSID: defaults.string(forKey: Key.homeSSID)
            )
        }
        nonmutating set {
            defaults.set(newValue.lanURL?.absoluteString, forKey: Key.lanURL)
            defaults.set(newValue.publicURL?.absoluteString, forKey: Key.publicURL)
            defaults.set(newValue.homeSSID, forKey: Key.homeSSID)
        }
    }

    var preferLAN: Bool {
        get { bool(Key.preferLAN, default: true) }
        nonmutating set { defaults.set(newValue, forKey: Key.preferLAN) }
    }

    var forceOffline: Bool {
        get { bool(Key.forceOffline, default: false) }
        nonmutating set { defaults.set(newValue, forKey: Key.forceOffline) }
    }

    var offlineFallback: Bool {
        get { bool(Key.offlineFallback, default: true) }
        nonmutating set { defaults.set(newValue, forKey: Key.offlineFallback) }
    }

    var copyInPCMode: Bool {
        get { bool(Key.copyInPCMode, default: false) }
        nonmutating set { defaults.set(newValue, forKey: Key.copyInPCMode) }
    }

    var offlineEngine: OfflineEnginePreference {
        get {
            defaults.string(forKey: Key.offlineEngine).flatMap(OfflineEnginePreference.init(rawValue:)) ?? .automatic
        }
        nonmutating set { defaults.set(newValue.rawValue, forKey: Key.offlineEngine) }
    }

    /// Cached copy of the hub's `GET /vocab`, used as on-device contextual strings.
    var vocabTerms: [String] {
        get { defaults.stringArray(forKey: Key.vocabTerms) ?? [] }
        nonmutating set { defaults.set(newValue, forKey: Key.vocabTerms) }
    }

    var vocabVersion: Int? {
        get { defaults.object(forKey: Key.vocabVersion) as? Int }
        nonmutating set { defaults.set(newValue, forKey: Key.vocabVersion) }
    }

    var vocabFetchedAt: Date? {
        get { defaults.object(forKey: Key.vocabFetchedAt) as? Date }
        nonmutating set { defaults.set(newValue, forKey: Key.vocabFetchedAt) }
    }

    /// The computer paired through the v2 flow (the device token is in the keychain).
    var pairedComputer: PairedComputer? {
        get { decode(PairedComputer.self, Key.pairedComputer) }
        nonmutating set { encode(newValue, Key.pairedComputer) }
    }

    /// The last `/info` of the paired computer.
    var cachedHubInfo: CachedHubInfo? {
        get { decode(CachedHubInfo.self, Key.hubInfo) }
        nonmutating set { encode(newValue, Key.hubInfo) }
    }

    /// First-launch setup finished (or skipped).
    var onboardingDone: Bool {
        get { bool(Key.onboardingDone, default: false) }
        nonmutating set { defaults.set(newValue, forKey: Key.onboardingDone) }
    }

    /// Advanced: use the legacy public (Cloudflare) route when the PC isn't on the LAN.
    var useTunnel: Bool {
        get { bool(Key.useTunnel, default: true) }
        nonmutating set { defaults.set(newValue, forKey: Key.useTunnel) }
    }

    /// A result waiting for the app to be in front so it can go on the clipboard.
    var pendingCopy: PendingCopy? {
        get { decode(PendingCopy.self, Key.pendingCopy) }
        nonmutating set { encode(newValue, Key.pendingCopy) }
    }

    /// A random id for this iPhone, created once, sent with pairing requests so the PC can
    /// recognise a re-pair. Not tied to the hardware or the Apple ID.
    func deviceID() -> String {
        if let existing = defaults.string(forKey: Key.deviceID), !existing.isEmpty {
            return existing
        }
        let created = UUID().uuidString.lowercased()
        defaults.set(created, forKey: Key.deviceID)
        return created
    }

    private func bool(_ key: String, default value: Bool) -> Bool {
        defaults.object(forKey: key) == nil ? value : defaults.bool(forKey: key)
    }

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

/// A "start dictating" request left by a process that can't start recording itself (the
/// widget extension). The app consumes it when it becomes active.
enum PendingDictation {
    static let maxAge: TimeInterval = 20

    static func request(in settings: SharedSettings = SharedSettings(), now: Date = Date()) {
        settings.defaults.set(now.timeIntervalSince1970, forKey: SharedSettings.Key.pendingDictationAt)
    }

    /// True (once) if a request newer than `maxAge` is waiting.
    static func consume(in settings: SharedSettings = SharedSettings(), now: Date = Date()) -> Bool {
        let key = SharedSettings.Key.pendingDictationAt
        let stamp = settings.defaults.double(forKey: key)
        guard stamp > 0 else { return false }
        settings.defaults.removeObject(forKey: key)
        let age = now.timeIntervalSince1970 - stamp
        return age >= -1 && age <= maxAge
    }
}

/// A dictation result that couldn't go on the clipboard yet.
///
/// iOS keeps apps in the background away from the clipboard, and a dictation started from the
/// Action Button, Control Center or the Live Activity finishes with ShoutAbout in the
/// background. So the app tries anyway, and when the pasteboard didn't take it, keeps the
/// text here (the App Group) and writes it the moment it's in front: a tap on the Live
/// Activity ("Tap here to copy it") opens it, or simply switching to it. Only for a short
/// while, so an old dictation never replaces something copied much later; after that the
/// result card's Copy button does it.
struct PendingCopy: Codable, Equatable, Sendable {
    static let maxAge: TimeInterval = 10 * 60

    var text: String
    var createdAt: Date

    func isFresh(at now: Date = Date()) -> Bool {
        let age = now.timeIntervalSince(createdAt)
        return age >= -60 && age <= Self.maxAge
    }
}

/// Kinds for the widget extension's controls and widgets (stable identifiers).
enum WidgetKinds {
    static let targetControl = "fyi.shoutabout.control.target"
    static let dictateControl = "fyi.shoutabout.control.dictate"
    static let lockScreen = "fyi.shoutabout.widget.dictate"
}
