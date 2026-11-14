import Foundation

// Scripted stand-ins for the network and the on-device recognizer, so the routing and pairing
// logic in Shared/Core runs in the tests exactly as in the app, with no network.

struct TestError: Error, LocalizedError {
    var message = "the on-device model isn't downloaded"
    var errorDescription: String? { message }
}

extension HelloResponse {
    static let sample = HelloResponse(app: "shoutabout", v: 1, id: "hub-1", name: "Sample-Desktop", version: "2.0.0", pairing: true)
}

extension HubInfo {
    static func sample(speed: String, accepts: [String] = ["audio", "text"]) -> HubInfo {
        HubInfo(
            id: "hub-1",
            name: "Sample-Desktop",
            version: "2.0.0",
            stt: HubInfo.STT(tier: "light", device: "cpu", speed: speed),
            cleanup: false,
            accepts: accepts
        )
    }
}

enum Samples {
    static let audio = URL(fileURLWithPath: "/tmp/sample-dictation.m4a")
    static let lan = URL(string: "http://192.168.1.20:8766")!
    static let tunnelURL = URL(string: "https://dictate.example.com")!
    static let phoneText = "Sample text from the phone."
    static let pcText = "Sample text from the PC."

    static func link(hubID: String? = "hub-1", tunnel: Bool = false, lan: URL = Samples.lan) -> ComputerLink {
        var link = ComputerLink(lanURL: lan, token: "device-token", hubID: hubID, tunnel: nil)
        if tunnel {
            link.tunnel = HubEndpoint(
                baseURL: tunnelURL,
                route: .tunnel,
                token: "legacy-token",
                cfClientID: "id.access",
                cfClientSecret: "secret"
            )
        }
        return link
    }

    static func request(
        target: DictationTarget = .pc,
        computer: ComputerLink? = Samples.link(),
        onWiFi: Bool = true,
        cachedInfo: HubInfo? = nil
    ) -> DictationRequest {
        DictationRequest(audioURL: audio, target: target, computer: computer, onWiFi: onWiFi, cachedInfo: cachedInfo)
    }
}

actor MockTransport: HubTransport {
    var helloResult: Result<HelloResponse, HubFailure>
    var healthResult: Result<Void, HubFailure>
    var infoResult: Result<HubInfo, HubFailure>
    var lanTranscribe: Result<TranscribeResponse, HubFailure>
    var tunnelTranscribe: Result<TranscribeResponse, HubFailure>
    var pasteResult: Result<PasteResponse, HubFailure>
    var pairResult: Result<PairRequestAccepted, HubFailure>
    var statuses: [Result<PairStatus, HubFailure>]

    private(set) var calls: [String] = []
    private(set) var pastedText: String?
    private(set) var pairBody: PairRequestBody?
    private(set) var tokensSent: [String] = []
    /// The `?target=` of every upload.
    private(set) var targetsSent: [DictationTarget] = []

    init(
        hello: Result<HelloResponse, HubFailure> = .success(.sample),
        health: Result<Void, HubFailure> = .success(()),
        info: Result<HubInfo, HubFailure> = .success(.sample(speed: "fast")),
        lanTranscribe: Result<TranscribeResponse, HubFailure> = .success(TranscribeResponse(text: Samples.pcText, pasted: true, route: "lan")),
        tunnelTranscribe: Result<TranscribeResponse, HubFailure> = .success(TranscribeResponse(text: Samples.pcText, pasted: true, route: "tunnel")),
        paste: Result<PasteResponse, HubFailure> = .success(PasteResponse(status: "ok", pasted: true)),
        pair: Result<PairRequestAccepted, HubFailure> = .success(PairRequestAccepted(requestID: "req-123", code: "4821", expiresIn: 120)),
        statuses: [Result<PairStatus, HubFailure>] = []
    ) {
        helloResult = hello
        healthResult = health
        infoResult = info
        self.lanTranscribe = lanTranscribe
        self.tunnelTranscribe = tunnelTranscribe
        pasteResult = paste
        pairResult = pair
        self.statuses = statuses
    }

    func hello(_ base: URL, timeout: TimeInterval) async throws -> HelloResponse {
        calls.append("hello")
        return try helloResult.get()
    }

    func health(_ base: URL, timeout: TimeInterval) async throws {
        calls.append("health")
        try healthResult.get()
    }

    func info(_ endpoint: HubEndpoint, timeout: TimeInterval) async throws -> HubInfo {
        calls.append("info")
        tokensSent.append(endpoint.token)
        return try infoResult.get()
    }

    func transcribe(fileURL: URL, target: DictationTarget, endpoint: HubEndpoint) async throws -> TranscribeResponse {
        calls.append("transcribe:\(endpoint.route.rawValue)")
        tokensSent.append(endpoint.token)
        targetsSent.append(target)
        return try (endpoint.route == .tunnel ? tunnelTranscribe : lanTranscribe).get()
    }

    func paste(_ text: String, endpoint: HubEndpoint) async throws -> PasteResponse {
        calls.append("paste")
        tokensSent.append(endpoint.token)
        pastedText = text
        return try pasteResult.get()
    }

    func requestPairing(_ base: URL, body: PairRequestBody) async throws -> PairRequestAccepted {
        calls.append("pair")
        pairBody = body
        return try pairResult.get()
    }

    func pairingStatus(_ base: URL, requestID: String) async throws -> PairStatus {
        calls.append("status")
        guard !statuses.isEmpty else { return .pending }
        return try statuses.removeFirst().get()
    }
}

actor MockOnDevice: OnDeviceTranscribing {
    var result: Result<OnDeviceResult, TestError>
    private(set) var count = 0

    init(_ result: Result<OnDeviceResult, TestError> = .success(OnDeviceResult(text: Samples.phoneText, engine: "SpeechTranscriber"))) {
        self.result = result
    }

    func transcribe(_ audioURL: URL) async throws -> OnDeviceResult {
        count += 1
        return try result.get()
    }
}

/// A hub's /stream/* endpoints: by default it takes chunks in order like the real one (a chunk
/// past the expected one is "missing"), and scripted answers can replace any step.
actor FakeStreamTransport: HubStreamTransport {
    var helloResult: Result<HelloResponse, HubFailure>
    var startResult: Result<StreamStarted, HubFailure>
    /// Answers for the chunk posts in order; when they run out, the hub answers itself.
    var chunkReplies: [Result<StreamChunkReply, HubFailure>]
    /// Answers for finish in order; when they run out, the text.
    var finishResults: [Result<TranscribeResponse, HubFailure>]
    /// Each chunk post takes this long (cancellable).
    var chunkDelay: Duration

    private(set) var calls: [String] = []
    /// What the hub has, in order.
    private(set) var received = Data()
    private(set) var chunkSizes: [Int] = []
    private(set) var tokensSent: [String] = []
    private var expected = 0

    init(
        hello: Result<HelloResponse, HubFailure> = .success(.sample),
        start: Result<StreamStarted, HubFailure> = .success(StreamStarted(streamID: "stream_ABC-123", maxChunkBytes: 262_144)),
        chunkReplies: [Result<StreamChunkReply, HubFailure>] = [],
        finish: [Result<TranscribeResponse, HubFailure>] = [],
        chunkDelay: Duration = .zero
    ) {
        helloResult = hello
        startResult = start
        self.chunkReplies = chunkReplies
        finishResults = finish
        self.chunkDelay = chunkDelay
    }

    func hello(_ base: URL, timeout: TimeInterval) async throws -> HelloResponse {
        calls.append("hello")
        return try helloResult.get()
    }

    func startStream(target: DictationTarget, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> StreamStarted {
        calls.append("start:\(target.rawValue)")
        tokensSent.append(endpoint.token)
        return try startResult.get()
    }

    func sendChunk(_ audio: Data, seq: Int, streamID: String, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> StreamChunkReply {
        calls.append("audio:\(seq)")
        if chunkDelay > .zero {
            try? await Task.sleep(for: chunkDelay)
            if Task.isCancelled { throw HubFailure.unreachable("cancelled") }
        }
        if !chunkReplies.isEmpty {
            return try chunkReplies.removeFirst().get()
        }
        if seq > expected { return .missing(nextSeq: expected) }
        if seq == expected {
            received.append(audio)
            chunkSizes.append(audio.count)
            expected += 1
        }
        return .accepted(nextSeq: expected)
    }

    func finishStream(streamID: String, chunks: Int, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> TranscribeResponse {
        calls.append("finish:\(chunks)")
        if !finishResults.isEmpty {
            return try finishResults.removeFirst().get()
        }
        return TranscribeResponse(text: Samples.pcText, pasted: true, route: "lan")
    }

    func cancelStream(streamID: String, endpoint: HubEndpoint) async {
        calls.append("delete")
    }
}

actor StageLog {
    private(set) var stages: [DictationStage] = []

    func add(_ stage: DictationStage) {
        stages.append(stage)
    }
}

actor Counter {
    private(set) var value = 0

    func tick() {
        value += 1
    }
}
