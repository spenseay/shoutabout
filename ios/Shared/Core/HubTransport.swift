import Foundation

/// Everything the phone asks the PC. The app implements it with URLSession (`LiveHubTransport`);
/// the unit tests use a scripted mock, so routing and pairing are tested without a network.
///
/// Every method throws `HubFailure` (anything else is treated as "no answer").
protocol HubTransport: Sendable {
    /// `GET /hello`, no credentials. `timeout` is short: it doubles as the reachability check.
    func hello(_ base: URL, timeout: TimeInterval) async throws -> HelloResponse
    /// `GET /health`, no credentials (PCs from before /hello).
    func health(_ base: URL, timeout: TimeInterval) async throws
    /// `GET /info` with the bearer token.
    func info(_ endpoint: HubEndpoint, timeout: TimeInterval) async throws -> HubInfo
    /// `POST /transcribe?target=..` with the recording as the body.
    func transcribe(fileURL: URL, target: DictationTarget, endpoint: HubEndpoint) async throws -> TranscribeResponse
    /// `POST /paste` with text this iPhone transcribed.
    func paste(_ text: String, endpoint: HubEndpoint) async throws -> PasteResponse
    /// `POST /pair/request`, no credentials.
    func requestPairing(_ base: URL, body: PairRequestBody) async throws -> PairRequestAccepted
    /// `GET /pair/status?request_id=..`, no credentials.
    func pairingStatus(_ base: URL, requestID: String) async throws -> PairStatus
}

/// The computer this iPhone is paired with through the v2 flow (Bonjour + Allow on the PC).
/// Stored in the App Group; the device token itself lives in the keychain.
struct PairedComputer: Codable, Equatable, Sendable {
    /// The PC's installation UUID from `/hello` and the Bonjour TXT record.
    var id: String
    var name: String
    var lanURL: URL
    var pairedAt: Date = Date()
}

/// Everything a dictation needs to reach the paired computer, resolved from settings and the
/// keychain when recording stops.
struct ComputerLink: Sendable, Equatable {
    /// Plain-HTTP base URL on the home network.
    var lanURL: URL?
    /// Bearer token for the LAN route: the device token, or the legacy hub token.
    var token: String?
    /// The PC's installation id; `/hello` must answer with it before the token is sent.
    var hubID: String?
    /// Advanced: the owner's Cloudflare route (legacy pairing only).
    var tunnel: HubEndpoint?

    var lanEndpoint: HubEndpoint? {
        guard let lanURL, let token, !token.isEmpty else { return nil }
        return HubEndpoint(baseURL: lanURL, route: .lan, token: token)
    }

    var isUsable: Bool { lanEndpoint != nil || tunnel != nil }

    /// Key for the `/info` cache.
    var hubKey: String? { hubID ?? lanURL?.absoluteString }

    /// Combines the v2 pairing (device token) with the legacy QR pairing (hub token, tunnel).
    /// The v2 pairing wins for the LAN route; the tunnel only exists with legacy pairing and
    /// only when the "use my tunnel when away" option is on.
    static func make(
        paired: PairedComputer?,
        deviceToken: String?,
        legacy: HubConfiguration,
        legacySecrets: HubSecrets?,
        useTunnel: Bool
    ) -> ComputerLink? {
        var link = ComputerLink()
        if let paired {
            link.lanURL = paired.lanURL
            link.hubID = paired.id
            link.token = deviceToken.nonBlank ?? (legacySecrets?.token).nonBlank
        } else if let lan = legacy.lanURL {
            link.lanURL = lan
            link.token = (legacySecrets?.token).nonBlank
        }
        if useTunnel, let publicURL = legacy.publicURL, let secrets = legacySecrets, !secrets.token.isEmpty {
            link.tunnel = HubEndpoint(
                baseURL: publicURL,
                route: .tunnel,
                token: secrets.token,
                cfClientID: secrets.cfClientID,
                cfClientSecret: secrets.cfClientSecret
            )
        }
        return link.isUsable ? link : nil
    }
}

/// A ShoutAbout PC found on the network with Bonjour (`_shoutabout._tcp`).
struct DiscoveredComputer: Identifiable, Equatable, Hashable, Sendable {
    /// The TXT `id` (installation UUID), or the service name for a PC that doesn't send one.
    var id: String
    var name: String
    var version: String?
    var serviceName: String
    /// TXT `v` is the protocol version; this app speaks v1.
    var isCompatible: Bool

    init(serviceName: String, txt: [String: String]) {
        self.serviceName = serviceName
        let id = txt["id"].nonBlank
        self.id = id ?? serviceName
        self.name = txt["name"].nonBlank ?? serviceName
        self.version = txt["ver"].nonBlank
        self.isCompatible = (txt["v"].nonBlank).map { $0 == "1" } ?? true
    }

    init(id: String, name: String, version: String?, serviceName: String, isCompatible: Bool = true) {
        self.id = id
        self.name = name
        self.version = version
        self.serviceName = serviceName
        self.isCompatible = isCompatible
    }

    /// One row per PC (a PC on Wi-Fi and Ethernet shows up twice), sorted by name.
    static func unique(_ items: [DiscoveredComputer]) -> [DiscoveredComputer] {
        var seen: Set<String> = []
        var result: [DiscoveredComputer] = []
        for item in items where seen.insert(item.id).inserted {
            result.append(item)
        }
        return result.sorted {
            let order = $0.name.localizedStandardCompare($1.name)
            return order == .orderedSame ? $0.id < $1.id : order == .orderedAscending
        }
    }
}
