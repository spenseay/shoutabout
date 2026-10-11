import Foundation

// Wire types for the v2 cross-device protocol (docs: the "Cross-device contract" in the v2
// brief, mirrored in docs/ios-app.md). Pure Foundation so the parsing is unit-tested.
//
//   GET  /hello                      (no auth, LAN only)  -> HelloResponse
//   POST /pair/request               (no auth, LAN only)  -> 202 PairRequestAccepted
//   GET  /pair/status?request_id=..  (no auth, LAN only)  -> PairStatus
//   GET  /info                       (bearer)             -> HubInfo
//   POST /paste {"text": ...}        (bearer)             -> PasteResponse

/// `GET /hello`: "is a ShoutAbout PC listening here, and which one?"
struct HelloResponse: Codable, Equatable, Sendable {
    var app: String
    var v: Int?
    /// The PC's installation UUID (stable across restarts and IP changes).
    var id: String?
    var name: String?
    var version: String?
    /// False when the PC has pairing turned off.
    var pairing: Bool?

    var isShoutAbout: Bool { app == "shoutabout" }
}

/// `POST /pair/request` body.
struct PairRequestBody: Encodable, Equatable, Sendable {
    /// The contract caps the name at 60 characters.
    static let maxNameLength = 60

    var deviceName: String
    var deviceID: String

    enum CodingKeys: String, CodingKey {
        case deviceName = "device_name"
        case deviceID = "device_id"
    }

    init(deviceName: String, deviceID: String) {
        let trimmed = deviceName.trimmingCharacters(in: .whitespacesAndNewlines)
        self.deviceName = String((trimmed.isEmpty ? "iPhone" : trimmed).prefix(Self.maxNameLength))
        self.deviceID = deviceID
    }
}

/// `POST /pair/request` 202 body. The PC shows `code` next to Allow / Don't allow.
struct PairRequestAccepted: Codable, Equatable, Sendable {
    var requestID: String
    var code: String
    var expiresIn: Int?

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case code
        case expiresIn = "expires_in"
    }
}

/// The `hub` object of an approved `/pair/status`.
struct PairedHubInfo: Codable, Equatable, Sendable {
    var id: String
    var name: String
    var lanURL: String?

    enum CodingKeys: String, CodingKey {
        case id, name
        case lanURL = "lan_url"
    }
}

/// `GET /pair/status`. The token comes back exactly once; afterwards the request is `claimed`.
enum PairStatus: Equatable, Sendable {
    case pending
    case denied
    case expired
    case claimed
    case approved(token: String, hub: PairedHubInfo)
    /// Anything this app doesn't understand (a newer PC, or an approved answer without a token).
    case unknown(String)

    private struct Wire: Decodable {
        var status: String
        var token: String?
        var hub: PairedHubInfo?
    }

    static func decode(_ data: Data) throws -> PairStatus {
        let wire = try JSONDecoder().decode(Wire.self, from: data)
        switch wire.status {
        case "pending": return .pending
        case "denied": return .denied
        case "expired": return .expired
        case "claimed": return .claimed
        case "approved":
            guard let token = wire.token, !token.isEmpty, let hub = wire.hub else {
                return .unknown("approved without a token")
            }
            return .approved(token: token, hub: hub)
        default:
            return .unknown(wire.status)
        }
    }
}

/// How quickly the PC turns audio into text. "slow" (CPU-only, light model) makes the phone
/// transcribe on-device and send only the text.
enum HubSpeed: String, Codable, Sendable {
    case fast
    case ok
    case slow
}

/// `GET /info`.
struct HubInfo: Codable, Equatable, Sendable {
    var id: String?
    var name: String?
    var version: String?
    var stt: STT?
    var cleanup: Bool?
    var accepts: [String]?

    struct STT: Codable, Equatable, Sendable {
        var tier: String?
        var device: String?
        var speed: String?
    }

    /// Unknown or missing speed counts as "ok" (send the audio, like before v2).
    var speed: HubSpeed { stt?.speed.flatMap(HubSpeed.init(rawValue:)) ?? .ok }

    /// The PC can paste text the phone transcribed itself (`POST /paste`).
    var acceptsText: Bool { accepts?.contains("text") ?? false }

    /// The phone should transcribe and send only the text.
    var prefersText: Bool { speed == .slow && acceptsText }
}

/// `POST /paste` body.
struct PasteBody: Encodable, Equatable, Sendable {
    /// The contract caps pasted text at 10 000 characters.
    static let maxLength = 10_000

    var text: String
}

/// `POST /paste` answer.
struct PasteResponse: Codable, Equatable, Sendable {
    var status: String?
    var pasted: Bool?
}

/// `/info`, remembered per computer so a dictation doesn't wait for it.
struct CachedHubInfo: Codable, Equatable, Sendable {
    static let maxAge: TimeInterval = 24 * 3600

    var info: HubInfo
    var fetchedAt: Date
    /// Which computer it describes (hub id, or the LAN URL for older PCs).
    var hubKey: String

    func isFresh(for key: String, now: Date = Date()) -> Bool {
        hubKey == key && now.timeIntervalSince(fetchedAt) < Self.maxAge && now >= fetchedAt.addingTimeInterval(-60)
    }
}
