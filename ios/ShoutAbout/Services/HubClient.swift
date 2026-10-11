import Foundation

struct HealthResult: Sendable, Equatable {
    var ok: Bool
    var status: Int?
    var message: String
    var latencyMs: Int
}

/// The real `HubTransport`: URLSession, ephemeral (no cookies, no cache, nothing on disk).
struct LiveHubTransport: HubTransport {
    func hello(_ base: URL, timeout: TimeInterval) async throws -> HelloResponse {
        var request = URLRequest(url: HubURL.endpoint(base, path: "hello"))
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return try await HubClient.sendJSON(request, as: HelloResponse.self)
    }

    func health(_ base: URL, timeout: TimeInterval) async throws {
        let endpoint = HubEndpoint(baseURL: base, route: .lan, token: "")
        let result = await HubClient.health(endpoint, timeout: timeout)
        guard result.ok else {
            if let status = result.status, status != 0 {
                throw HubFailure.hub(status: status, detail: result.message)
            }
            throw HubFailure.unreachable(result.message)
        }
    }

    func info(_ endpoint: HubEndpoint, timeout: TimeInterval) async throws -> HubInfo {
        var request = URLRequest(url: HubURL.endpoint(endpoint.baseURL, path: "info"))
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        endpoint.authorize(&request)
        return try await HubClient.sendJSON(request, as: HubInfo.self)
    }

    func transcribe(fileURL: URL, target: DictationTarget, endpoint: HubEndpoint) async throws -> TranscribeResponse {
        try await HubClient.transcribe(fileURL: fileURL, target: target, endpoint: endpoint)
    }

    func paste(_ text: String, endpoint: HubEndpoint) async throws -> PasteResponse {
        var request = URLRequest(url: HubURL.endpoint(endpoint.baseURL, path: "paste"))
        request.httpMethod = "POST"
        request.timeoutInterval = endpoint.route == .lan ? 6 : 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try? JSONEncoder().encode(PasteBody(text: text))
        endpoint.authorize(&request)
        return try await HubClient.sendJSON(request, as: PasteResponse.self)
    }

    func requestPairing(_ base: URL, body: PairRequestBody) async throws -> PairRequestAccepted {
        var request = URLRequest(url: HubURL.endpoint(base, path: "pair/request"))
        request.httpMethod = "POST"
        request.timeoutInterval = 6
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try? JSONEncoder().encode(body)
        return try await HubClient.sendJSON(request, as: PairRequestAccepted.self)
    }

    func pairingStatus(_ base: URL, requestID: String) async throws -> PairStatus {
        let url = HubURL.endpoint(base, path: "pair/status", query: [URLQueryItem(name: "request_id", value: requestID)])
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 4
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, http) = try await HubClient.send(request)
        guard (200..<300).contains(http.statusCode) else {
            throw HubClient.failure(status: http.statusCode, body: data)
        }
        do {
            return try PairStatus.decode(data)
        } catch {
            throw HubFailure.badResponse("unexpected JSON")
        }
    }
}

/// HTTP helpers for the hub API. Stateless; safe to call from any actor.
enum HubClient {
    /// Ephemeral: no cookies, no cache, nothing written to disk.
    static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 300
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpAdditionalHeaders = ["User-Agent": "ShoutAbout-iOS/2"]
        return URLSession(configuration: config)
    }()

    /// LAN probe timeout: long enough for a Wi-Fi round trip, short enough to fall back fast.
    static let lanProbeTimeout: TimeInterval = 1.5
    static let tunnelProbeTimeout: TimeInterval = 6
    /// Upload + STT + cleanup for up to 10 minutes of audio.
    static let transcribeTimeout: TimeInterval = 90

    // MARK: GET /health

    /// `GET /health`. On the LAN route it sends NO credentials: it only checks that a hub
    /// answers before the token is sent over plain HTTP.
    static func health(_ endpoint: HubEndpoint, timeout: TimeInterval) async -> HealthResult {
        var request = URLRequest(url: HubURL.endpoint(endpoint.baseURL, path: "health"))
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        endpoint.addCloudflareHeaders(&request)
        let started = ContinuousClock.now
        do {
            let (data, response) = try await session.data(for: request)
            let ms = elapsedMs(since: started)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 200, isHealthyBody(data) {
                return HealthResult(ok: true, status: status, message: "OK", latencyMs: ms)
            }
            return HealthResult(ok: false, status: status, message: failure(status: status, body: data).userMessage, latencyMs: ms)
        } catch {
            return HealthResult(ok: false, status: nil, message: describe(error), latencyMs: elapsedMs(since: started))
        }
    }

    static func isHealthyBody(_ data: Data) -> Bool {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return object["ok"] as? Bool == true
    }

    // MARK: POST /transcribe

    /// Uploads the recording as the raw request body (`?target=`). Falls back to a multipart
    /// form upload once if the hub predates raw uploads.
    static func transcribe(fileURL: URL, target: DictationTarget, endpoint: HubEndpoint) async throws -> TranscribeResponse {
        var request = URLRequest(url: HubURL.transcribe(endpoint.baseURL, target: target))
        request.httpMethod = "POST"
        request.timeoutInterval = transcribeTimeout
        request.setValue("audio/x-m4a", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        endpoint.authorize(&request)

        let first = await upload(request, fromFile: fileURL)
        switch first {
        case .success(let response):
            return response
        case .failure(let failure) where HubResponseParser.isLegacyMultipartOnly(failure):
            break
        case .failure(let failure):
            throw failure
        }

        // Older hub: multipart/form-data with `file` + `target`.
        let boundary = "ShoutAbout-\(UUID().uuidString)"
        let audio: Data
        do {
            audio = try Data(contentsOf: fileURL)
        } catch {
            throw HubFailure.badResponse("couldn't read the recording")
        }
        let body = MultipartBody.make(boundary: boundary, target: target, audio: audio)
        let formURL = FileManager.default.temporaryDirectory.appendingPathComponent("upload-\(boundary).bin")
        do {
            try body.write(to: formURL)
        } catch {
            throw HubFailure.badResponse("couldn't stage the upload")
        }
        defer { try? FileManager.default.removeItem(at: formURL) }
        var form = URLRequest(url: HubURL.endpoint(endpoint.baseURL, path: "transcribe"))
        form.httpMethod = "POST"
        form.timeoutInterval = transcribeTimeout
        form.setValue(MultipartBody.contentType(boundary: boundary), forHTTPHeaderField: "Content-Type")
        form.setValue("application/json", forHTTPHeaderField: "Accept")
        endpoint.authorize(&form)
        return try await upload(form, fromFile: formURL).get()
    }

    private static func upload(_ request: URLRequest, fromFile fileURL: URL) async -> Result<TranscribeResponse, HubFailure> {
        do {
            let (data, response) = try await session.upload(for: request, fromFile: fileURL)
            guard let http = response as? HTTPURLResponse else {
                return .failure(.badResponse("not HTTP"))
            }
            return HubResponseParser.parseTranscribe(
                status: http.statusCode,
                body: data,
                contentType: http.value(forHTTPHeaderField: "Content-Type")
            )
        } catch {
            return .failure(.unreachable(describe(error)))
        }
    }

    // MARK: GET/POST /vocab

    static func fetchVocab(_ endpoint: HubEndpoint) async throws -> VocabList {
        var request = URLRequest(url: HubURL.endpoint(endpoint.baseURL, path: "vocab"))
        request.httpMethod = "GET"
        request.timeoutInterval = endpoint.route == .lan ? 4 : 10
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        endpoint.authorize(&request)
        return try await sendJSON(request, as: VocabList.self)
    }

    static func addVocab(_ term: String, endpoint: HubEndpoint) async throws -> VocabAddResult {
        var request = URLRequest(url: HubURL.endpoint(endpoint.baseURL, path: "vocab"))
        request.httpMethod = "POST"
        request.timeoutInterval = endpoint.route == .lan ? 4 : 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["term": term])
        endpoint.authorize(&request)
        return try await sendJSON(request, as: VocabAddResult.self)
    }

    // MARK: Plumbing

    /// Sends a request; any transport error becomes `HubFailure.unreachable`.
    static func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let result: (Data, URLResponse)
        do {
            result = try await session.data(for: request)
        } catch {
            throw HubFailure.unreachable(describe(error))
        }
        let (data, response) = result
        guard let http = response as? HTTPURLResponse else {
            throw HubFailure.badResponse("not HTTP")
        }
        return (data, http)
    }

    /// Sends a request and decodes a 2xx JSON answer (errors become `HubFailure`).
    static func sendJSON<T: Decodable & Sendable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let (data, http) = try await send(request)
        return try HubResponseParser.parse(
            T.self,
            status: http.statusCode,
            body: data,
            contentType: http.value(forHTTPHeaderField: "Content-Type")
        ).get()
    }

    static func failure(status: Int, body: Data) -> HubFailure {
        if let detail = HubResponseParser.errorDetail(body) {
            return .hub(status: status, detail: detail)
        }
        return .gateway(status: status, snippet: HubResponseParser.snippet(body))
    }

    static func describe(_ error: any Error) -> String {
        guard let urlError = error as? URLError else { return error.localizedDescription }
        switch urlError.code {
        case .timedOut: return "timed out"
        case .cannotConnectToHost: return "connection refused"
        case .cannotFindHost, .dnsLookupFailed: return "host not found"
        case .notConnectedToInternet: return "no network"
        case .networkConnectionLost: return "connection lost"
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot:
            return "TLS error"
        case .appTransportSecurityRequiresSecureConnection: return "blocked by App Transport Security"
        case .cancelled: return "cancelled"
        default: return urlError.localizedDescription
        }
    }

    static func elapsedMs(since start: ContinuousClock.Instant) -> Int {
        let elapsed = ContinuousClock.now - start
        let (seconds, attoseconds) = elapsed.components
        return Int(seconds * 1000 + attoseconds / 1_000_000_000_000_000)
    }
}
