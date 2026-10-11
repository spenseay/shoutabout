import Foundation

/// Non-secret hub settings (stored in the App Group's UserDefaults).
struct HubConfiguration: Equatable, Sendable, Codable {
    /// `http://<pc-lan-ip>:<port>`: plain HTTP to a private address on the home network.
    var lanURL: URL?
    /// `https://dictate.example.com`: through Cloudflare Tunnel + Access.
    var publicURL: URL?
    /// Home Wi-Fi name. Informational for now: reading the current SSID needs Location
    /// permission and the Access Wi-Fi Information entitlement, so routing doesn't use it.
    var homeSSID: String?

    var isPaired: Bool { lanURL != nil || publicURL != nil }
}

/// Hub secrets (stored in the keychain, never in UserDefaults or the repo).
struct HubSecrets: Equatable, Sendable {
    var token: String
    var cfClientID: String?
    var cfClientSecret: String?

    var hasCloudflareToken: Bool { cfClientID != nil && cfClientSecret != nil }
}

enum PairingError: Error, Equatable, Sendable, LocalizedError {
    case notJSON
    case notShoutAbout
    case unsupportedVersion(Int)
    case missingToken
    case noURL
    case invalidURL(field: String)
    case insecurePublicURL
    case lanURLNotPrivate
    case incompleteCloudflareToken

    var errorDescription: String? {
        switch self {
        case .notJSON:
            return "That QR code isn't a ShoutAbout pairing code."
        case .notShoutAbout:
            return "That QR code isn't a ShoutAbout pairing code (no \"shoutabout\" version)."
        case .unsupportedVersion(let version):
            return "This pairing code is version \(version); update the app to use it."
        case .missingToken:
            return "The pairing code has no hub token."
        case .noURL:
            return "The pairing code has neither a LAN URL nor a public URL."
        case .invalidURL(let field):
            return "The \(field) isn't a valid http(s) URL."
        case .insecurePublicURL:
            return "The public URL must start with https://."
        case .lanURLNotPrivate:
            return "A plain-http LAN URL must point at a private address (192.168.x.x, 10.x.x.x, …)."
        case .incompleteCloudflareToken:
            return "The Cloudflare service token needs both the Client ID and the Client Secret."
        }
    }
}

/// The pairing QR shown by `python -m shoutabout pair`:
///
///     {"shoutabout": 1, "lan_url": "http://192.168.1.50:8765",
///      "public_url": "https://dictate.shoutabout.fyi" | null, "token": "<hub token>",
///      "cf_client_id": "..." (optional), "cf_client_secret": "..." (optional),
///      "ssid": "<home wifi>" | null}
struct PairingPayload: Equatable, Sendable {
    static let supportedVersion = 1

    var configuration: HubConfiguration
    var secrets: HubSecrets

    private struct Wire: Decodable {
        var shoutabout: Int?
        var lanURL: String?
        var publicURL: String?
        var token: String?
        var cfClientID: String?
        var cfClientSecret: String?
        var ssid: String?

        enum CodingKeys: String, CodingKey {
            case shoutabout
            case lanURL = "lan_url"
            case publicURL = "public_url"
            case token
            case cfClientID = "cf_client_id"
            case cfClientSecret = "cf_client_secret"
            case ssid
        }
    }

    /// Parses and validates a scanned (or pasted) pairing code.
    static func parse(_ text: String) throws -> PairingPayload {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("{"), let data = trimmed.data(using: .utf8) else {
            throw PairingError.notJSON
        }
        let wire: Wire
        do {
            wire = try JSONDecoder().decode(Wire.self, from: data)
        } catch {
            throw PairingError.notJSON
        }
        guard let version = wire.shoutabout else { throw PairingError.notShoutAbout }
        guard version == supportedVersion else { throw PairingError.unsupportedVersion(version) }
        return try make(
            lanURL: wire.lanURL,
            publicURL: wire.publicURL,
            token: wire.token,
            cfClientID: wire.cfClientID,
            cfClientSecret: wire.cfClientSecret,
            ssid: wire.ssid
        )
    }

    /// Validates values from the QR code or the manual entry form.
    static func make(
        lanURL: String?,
        publicURL: String?,
        token: String?,
        cfClientID: String?,
        cfClientSecret: String?,
        ssid: String?
    ) throws -> PairingPayload {
        guard let token = token.nonBlank else { throw PairingError.missingToken }

        var lan: URL?
        if let raw = lanURL.nonBlank {
            guard let url = URLRules.normalizedBaseURL(raw, defaultScheme: "http") else {
                throw PairingError.invalidURL(field: "LAN URL")
            }
            if url.scheme == "http", !URLRules.isPrivateHost(url.host() ?? "") {
                throw PairingError.lanURLNotPrivate
            }
            lan = url
        }

        var pub: URL?
        if let raw = publicURL.nonBlank {
            guard let url = URLRules.normalizedBaseURL(raw, defaultScheme: "https") else {
                throw PairingError.invalidURL(field: "public URL")
            }
            guard url.scheme == "https" else { throw PairingError.insecurePublicURL }
            pub = url
        }

        guard lan != nil || pub != nil else { throw PairingError.noURL }

        let cfID = cfClientID.nonBlank
        let cfSecret = cfClientSecret.nonBlank
        guard (cfID == nil) == (cfSecret == nil) else { throw PairingError.incompleteCloudflareToken }

        return PairingPayload(
            configuration: HubConfiguration(lanURL: lan, publicURL: pub, homeSSID: ssid.nonBlank),
            secrets: HubSecrets(token: token, cfClientID: cfID, cfClientSecret: cfSecret)
        )
    }

    /// True for the bare token QR from `python -m shoutabout token --qr` (43 URL-safe chars),
    /// so the scanner can offer "use as hub token" instead of failing.
    static func looksLikeBareToken(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (32...128).contains(trimmed.count) else { return false }
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        return trimmed.allSatisfy { allowed.contains($0) }
    }
}

enum URLRules {
    /// Accepts "http(s)://host[:port][/prefix]" (also with a pasted "/transcribe" or "/health"
    /// suffix or trailing slashes) and returns the base URL the client appends paths to.
    static func normalizedBaseURL(_ raw: String, defaultScheme: String? = nil) -> URL? {
        var trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let defaultScheme, !trimmed.contains("://"), !trimmed.isEmpty {
            trimmed = "\(defaultScheme)://\(trimmed)"
        }
        guard
            var parts = URLComponents(string: trimmed),
            let scheme = parts.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            let host = parts.host, !host.isEmpty
        else { return nil }
        parts.scheme = scheme
        parts.query = nil
        parts.fragment = nil
        parts.user = nil
        parts.password = nil
        var path = parts.path
        while path.hasSuffix("/") { path.removeLast() }
        for suffix in ["/transcribe", "/health", "/vocab", "/hello", "/info"] where path.lowercased().hasSuffix(suffix) {
            path.removeLast(suffix.count)
        }
        while path.hasSuffix("/") { path.removeLast() }
        parts.path = path
        return parts.url
    }

    /// Private, link-local, CGNAT (Tailscale) and loopback IPs, `.local`/`.lan`/`.home.arpa`
    /// names and unqualified host names: the places a plain-http LAN URL may point to.
    static func isPrivateHost(_ rawHost: String) -> Bool {
        var host = rawHost.lowercased()
        if host.hasPrefix("[") && host.hasSuffix("]") {
            host = String(host.dropFirst().dropLast())
        }
        if host.isEmpty { return false }
        if let octets = ipv4Octets(host) {
            let (a, b) = (octets[0], octets[1])
            if a == 10 || a == 127 { return true }
            if a == 172 && (16...31).contains(b) { return true }
            if a == 192 && b == 168 { return true }
            if a == 169 && b == 254 { return true }
            if a == 100 && (64...127).contains(b) { return true }
            return false
        }
        if host.contains(":") {
            // IPv6: unique local fc00::/7, link-local fe80::/10, loopback ::1.
            if host == "::1" { return true }
            if host.hasPrefix("fc") || host.hasPrefix("fd") { return true }
            if host.hasPrefix("fe8") || host.hasPrefix("fe9") || host.hasPrefix("fea") || host.hasPrefix("feb") {
                return true
            }
            return false
        }
        if !host.contains(".") { return true }  // unqualified name, resolved by the local DNS
        for suffix in [".local", ".lan", ".home.arpa", ".internal", ".localhost"] where host.hasSuffix(suffix) {
            return true
        }
        return false
    }

    /// Carrier-grade NAT addresses (100.64.0.0/10), which VPNs like Tailscale use: reachable
    /// away from home, so the LAN check is still worth trying on cellular.
    static func isVPNHost(_ rawHost: String) -> Bool {
        guard let octets = ipv4Octets(rawHost.lowercased()) else { return false }
        return octets[0] == 100 && (64...127).contains(octets[1])
    }

    /// "192.168.1.20:8766", "http://pc.local:8766" -> a plain-http LAN base URL, or nil when it
    /// isn't one (public hosts must not get the token over plain HTTP).
    static func lanBaseURL(_ raw: String) -> URL? {
        guard let url = normalizedBaseURL(raw, defaultScheme: "http") else { return nil }
        if url.scheme == "http", !isPrivateHost(url.host() ?? "") { return nil }
        return url
    }

    static func ipv4Octets(_ host: String) -> [Int]? {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var octets: [Int] = []
        for part in parts {
            guard !part.isEmpty, part.count <= 3, part.allSatisfy(\.isNumber), let value = Int(part), value <= 255 else {
                return nil
            }
            octets.append(value)
        }
        return octets
    }
}

extension Optional where Wrapped == String {
    /// The trimmed string, or nil when it is nil/empty/whitespace.
    var nonBlank: String? {
        guard let trimmed = self?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
            return nil
        }
        return trimmed
    }
}
