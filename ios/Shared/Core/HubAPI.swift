import Foundation

// Wire types and response handling for the ShoutAbout hub (src/shoutabout/server.py).
// Pure Foundation so it can be unit-tested without a network.

/// `POST /transcribe` success body.
struct TranscribeResponse: Codable, Equatable, Sendable {
    var text: String
    var rawText: String?
    var target: String?
    var pasted: Bool?
    var warning: String?
    /// "ok" | "warning" (hubs from before the field existed omit it).
    var status: String?
    var route: String?
    var cleanup: String?
    var timingsMs: Timings?

    struct Timings: Codable, Equatable, Sendable {
        var decode: Double?
        var stt: Double?
        var cleanup: Double?
        var paste: Double?
        var total: Double?
    }

    enum CodingKeys: String, CodingKey {
        case text
        case rawText = "raw_text"
        case target, pasted, warning, status, route, cleanup
        case timingsMs = "timings_ms"
    }

    /// The hub's own route label, mapped to ours (nil if unknown).
    var hubRoute: Route? { route.flatMap(Route.init(rawValue:)) }
}

/// `GET /vocab` body.
struct VocabList: Codable, Equatable, Sendable {
    var terms: [String]
    /// "say -> write" lines from vocab.txt; nil from a PC that predates them.
    var replacements: [VocabReplacement]?
    var version: Int?
}

/// A replacement: what you say, and what gets written instead ("omw" -> "on my way").
struct VocabReplacement: Codable, Equatable, Hashable, Sendable {
    var say: String
    var write: String
}

/// `POST /vocab` body.
struct VocabAddResult: Codable, Equatable, Sendable {
    var added: Bool
    var term: String
    var count: Int?
}

/// Why a hub request didn't produce a result. Drives both the message shown to the user and
/// whether the app falls back to another route (see `RoutePlanner.action(after:)`).
enum HubFailure: Error, Equatable, Sendable {
    /// No HTTP answer at all: timeout, refused, no route, DNS, TLS, connection lost.
    case unreachable(String)
    /// A JSON `{"detail": ...}` error from the hub itself.
    case hub(status: Int, detail: String)
    /// A non-JSON answer: Cloudflare Access (401 HTML), a tunnel error page (502/530), a proxy.
    case gateway(status: Int, snippet: String)
    /// 2xx, but the body wasn't the expected JSON.
    case badResponse(String)

    /// Any error from a transport call, as a HubFailure (URLSession errors are mapped by the
    /// client already; anything else counts as "no answer").
    static func from(_ error: any Error) -> HubFailure {
        (error as? HubFailure) ?? .unreachable(error.localizedDescription)
    }

    /// The HTTP status of a hub or gateway answer (nil when nothing answered).
    var status: Int? {
        switch self {
        case .hub(let status, _), .gateway(let status, _): return status
        case .unreachable, .badResponse: return nil
        }
    }

    var userMessage: String {
        switch self {
        case .unreachable(let why):
            return "Couldn't reach your PC (\(why))."
        case .hub(let status, let detail):
            switch status {
            case 401: return "Your PC didn't accept this iPhone. Connect again in Settings."
            case 403: return "Your PC refused the request: \(detail)."
            case 409: return "Your PC is paused: \(detail)."
            case 413: return "Too long for your PC: \(detail)."
            case 429: return "Slow down: \(detail)."
            case 503: return "Your PC isn't ready yet: \(detail)."
            default: return "Your PC answered with error \(status): \(detail)"
            }
        case .gateway(let status, _):
            switch status {
            case 401, 403:
                return "Cloudflare Access blocked it (\(status)). Check the service token."
            case 502, 504, 521, 522, 523, 524, 530:
                return "Your PC didn't answer (\(status)). Is ShoutAbout running?"
            default:
                return "Unexpected answer (\(status))."
            }
        case .badResponse(let why):
            return "Your PC's answer couldn't be read (\(why))."
        }
    }
}

/// One way to reach the hub, with the credentials that route needs.
struct HubEndpoint: Sendable, Equatable {
    var baseURL: URL
    var route: Route
    /// The device token from pairing, or the legacy hub token.
    var token: String
    var cfClientID: String?
    var cfClientSecret: String?

    /// Adds the bearer token (and the Cloudflare Access service token on the tunnel route).
    func authorize(_ request: inout URLRequest) {
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        addCloudflareHeaders(&request)
    }

    func addCloudflareHeaders(_ request: inout URLRequest) {
        guard route == .tunnel, let cfClientID, let cfClientSecret else { return }
        request.setValue(cfClientID, forHTTPHeaderField: "CF-Access-Client-Id")
        request.setValue(cfClientSecret, forHTTPHeaderField: "CF-Access-Client-Secret")
    }
}

enum HubResponseParser {
    /// Classifies an HTTP answer to `/transcribe`.
    static func parseTranscribe(status: Int, body: Data, contentType: String?) -> Result<TranscribeResponse, HubFailure> {
        parse(TranscribeResponse.self, status: status, body: body, contentType: contentType)
    }

    static func parse<T: Decodable>(_ type: T.Type, status: Int, body: Data, contentType: String?) -> Result<T, HubFailure> {
        if (200..<300).contains(status) {
            do {
                return .success(try JSONDecoder().decode(T.self, from: body))
            } catch {
                if !looksLikeJSON(body: body, contentType: contentType) {
                    return .failure(.badResponse("not JSON"))
                }
                return .failure(.badResponse("unexpected JSON"))
            }
        }
        if let detail = errorDetail(body) {
            return .failure(.hub(status: status, detail: detail))
        }
        return .failure(.gateway(status: status, snippet: snippet(body)))
    }

    /// The `detail` of a FastAPI-style error body, if the body is one.
    static func errorDetail(_ body: Data) -> String? {
        guard
            let object = try? JSONSerialization.jsonObject(with: body),
            let dict = object as? [String: Any],
            let detail = dict["detail"]
        else { return nil }
        if let text = detail as? String { return text }
        // FastAPI validation errors carry a list; keep something readable.
        if let data = try? JSONSerialization.data(withJSONObject: detail),
           let text = String(data: data, encoding: .utf8) {
            return text
        }
        return String(describing: detail)
    }

    static func looksLikeJSON(body: Data, contentType: String?) -> Bool {
        if let contentType, contentType.lowercased().contains("json") { return true }
        guard let first = body.first(where: { !($0 == 0x20 || $0 == 0x0A || $0 == 0x0D || $0 == 0x09) }) else {
            return false
        }
        return first == UInt8(ascii: "{") || first == UInt8(ascii: "[")
    }

    /// First line or so of a non-JSON body, for logs/diagnostics (never shown raw to the user).
    static func snippet(_ body: Data, limit: Int = 120) -> String {
        let text = String(decoding: body.prefix(2048), as: UTF8.self)
        let collapsed = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        return String(collapsed.prefix(limit))
    }

    /// The hub answers this to a raw-body upload when it predates raw uploads (multipart only).
    static func isLegacyMultipartOnly(_ failure: HubFailure) -> Bool {
        if case .hub(status: 400, detail: let detail) = failure {
            return detail.contains("missing 'file' upload") || detail.lowercased().contains("multipart")
        }
        return false
    }
}

/// multipart/form-data body for hubs that only accept form uploads (`file` + `target`).
enum MultipartBody {
    static func make(boundary: String, target: DictationTarget, audio: Data, filename: String = "dictation.m4a") -> Data {
        var body = Data()
        func line(_ text: String) { body.append(Data((text + "\r\n").utf8)) }
        line("--\(boundary)")
        line("Content-Disposition: form-data; name=\"target\"")
        line("")
        line(target.rawValue)
        line("--\(boundary)")
        line("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"")
        line("Content-Type: audio/x-m4a")
        line("")
        body.append(audio)
        line("")
        line("--\(boundary)--")
        return body
    }

    static func contentType(boundary: String) -> String {
        "multipart/form-data; boundary=\(boundary)"
    }
}

/// Builds hub URLs from a base URL (scheme + host + port, maybe a path prefix).
enum HubURL {
    static func endpoint(_ base: URL, path: String, query: [URLQueryItem] = []) -> URL {
        var url = base
        for component in path.split(separator: "/") {
            url.append(path: String(component))
        }
        guard !query.isEmpty, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return url
        }
        parts.queryItems = (parts.queryItems ?? []) + query
        return parts.url ?? url
    }

    static func transcribe(_ base: URL, target: DictationTarget) -> URL {
        endpoint(base, path: "transcribe", query: [URLQueryItem(name: "target", value: target.rawValue)])
    }
}
