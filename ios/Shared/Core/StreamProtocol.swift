import Foundation

// Transcribe while you talk: the hub's /stream/* endpoints (docs/ios-app.md, "Transcribe while
// you talk"). Wire types, URLs and answer parsing; pure Foundation so it's unit-tested. The
// session that drives them is StreamSession.swift; the app's input tap and URLSession calls are
// ShoutAbout/Services/StreamingUploader.swift.
//
//   POST   /stream/start {"target","sample_rate":16000,"encoding":"s16le"}
//            -> 200 {"stream_id","max_chunk_bytes"} | 503 model not loaded | 429 too many
//   POST   /stream/{id}/audio?seq=n   (raw s16le body, seq from 0)
//            -> 200 {"next_seq","received_s"} | 409 {"next_seq"} missing chunk | 404 | 413
//   POST   /stream/{id}/finish {"chunks": N}
//            -> 200 the /transcribe answer (+ "streamed": true), idempotent | 409 | 503
//   DELETE /stream/{id}  -> 204
//
// All authenticated like POST /transcribe (bearer). This iPhone uses them only on the LAN.

/// The audio the hub takes: 16 kHz mono, signed 16-bit little-endian.
enum StreamAudio {
    static let sampleRate = 16_000
    static let encoding = "s16le"
    static let bytesPerSecond = sampleRate * 2
    /// About 2 s per chunk.
    static let chunkBytes = 2 * bytesPerSecond
}

extension HubInfo {
    /// The PC transcribes while you talk (`/info` lists "stream" in `accepts`).
    var acceptsStream: Bool { accepts?.contains("stream") ?? false }
}

/// `POST /stream/start` body.
struct StreamStartBody: Encodable, Equatable, Sendable {
    var target: String
    var sampleRate: Int
    var encoding: String

    enum CodingKeys: String, CodingKey {
        case target
        case sampleRate = "sample_rate"
        case encoding
    }

    init(target: DictationTarget) {
        self.target = target.rawValue
        sampleRate = StreamAudio.sampleRate
        encoding = StreamAudio.encoding
    }
}

/// `POST /stream/start` answer.
struct StreamStarted: Codable, Equatable, Sendable {
    var streamID: String
    var maxChunkBytes: Int?

    enum CodingKeys: String, CodingKey {
        case streamID = "stream_id"
        case maxChunkBytes = "max_chunk_bytes"
    }

    /// The id goes into URL paths: only url-safe characters (the hub sends 32 of them).
    static func isSafeID(_ id: String) -> Bool {
        guard (1...128).contains(id.utf8.count) else { return false }
        return id.utf8.allSatisfy { byte in
            switch byte {
            case UInt8(ascii: "0")...UInt8(ascii: "9"),
                 UInt8(ascii: "A")...UInt8(ascii: "Z"),
                 UInt8(ascii: "a")...UInt8(ascii: "z"),
                 UInt8(ascii: "-"),
                 UInt8(ascii: "_"):
                return true
            default:
                return false
            }
        }
    }
}

/// `POST /stream/{id}/audio` 200 body.
struct StreamChunkAck: Codable, Equatable, Sendable {
    var nextSeq: Int
    var receivedS: Double?

    enum CodingKeys: String, CodingKey {
        case nextSeq = "next_seq"
        case receivedS = "received_s"
    }
}

/// `POST /stream/{id}/finish` body: how many chunks were sent (the next seq).
struct StreamFinishBody: Encodable, Equatable, Sendable {
    var chunks: Int
}

/// What became of one chunk.
enum StreamChunkReply: Equatable, Sendable {
    /// 200: the hub has every chunk before `nextSeq` (a resent one it already had is ignored).
    case accepted(nextSeq: Int)
    /// 409: a chunk before this one never arrived; the hub wants `nextSeq` next.
    case missing(nextSeq: Int)
    /// No usable answer.
    case failed(retryable: Bool)

    /// A failed chunk request: worth another try? (no answer, a garbled one, rate limit or a
    /// server error: yes; the stream is gone (404), too long (413), not allowed: no).
    static func after(_ failure: HubFailure) -> StreamChunkReply {
        switch failure {
        case .unreachable, .badResponse:
            return .failed(retryable: true)
        case .hub(let status, _), .gateway(let status, _):
            return .failed(retryable: status == 408 || status == 429 || status >= 500)
        }
    }

    /// The hub's answer to `POST /stream/{id}/audio`: `.accepted` or `.missing`, otherwise the
    /// failure.
    static func parse(status: Int, body: Data, contentType: String?) -> Result<StreamChunkReply, HubFailure> {
        if status == 409, let next = try? JSONDecoder().decode(Missing.self, from: body).nextSeq {
            return .success(.missing(nextSeq: next))
        }
        return HubResponseParser.parse(StreamChunkAck.self, status: status, body: body, contentType: contentType)
            .map { ack in StreamChunkReply.accepted(nextSeq: ack.nextSeq) }
    }

    private struct Missing: Decodable {
        var nextSeq: Int

        enum CodingKeys: String, CodingKey {
            case nextSeq = "next_seq"
        }
    }
}

/// The stream endpoints under a hub base URL.
enum StreamURL {
    static func start(_ base: URL) -> URL {
        HubURL.endpoint(base, path: "stream/start")
    }

    static func audio(_ base: URL, id: String, seq: Int) -> URL {
        HubURL.endpoint(base, path: "stream/\(id)/audio", query: [URLQueryItem(name: "seq", value: String(seq))])
    }

    static func finish(_ base: URL, id: String) -> URL {
        HubURL.endpoint(base, path: "stream/\(id)/finish")
    }

    /// DELETE: cancel.
    static func stream(_ base: URL, id: String) -> URL {
        HubURL.endpoint(base, path: "stream/\(id)")
    }
}

/// The calls a stream makes. The app implements it with URLSession (`LiveHubTransport`,
/// StreamingUploader.swift); the unit tests with a scripted hub. Methods throw `HubFailure`.
protocol HubStreamTransport: Sendable {
    /// `GET /hello`, no credentials: the token only goes to the paired PC.
    func hello(_ base: URL, timeout: TimeInterval) async throws -> HelloResponse
    func startStream(target: DictationTarget, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> StreamStarted
    /// `.accepted` or `.missing`; anything else throws.
    func sendChunk(_ audio: Data, seq: Int, streamID: String, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> StreamChunkReply
    /// The `/transcribe` answer.
    func finishStream(streamID: String, chunks: Int, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> TranscribeResponse
    /// `DELETE`, best effort (never throws).
    func cancelStream(streamID: String, endpoint: HubEndpoint) async
}
