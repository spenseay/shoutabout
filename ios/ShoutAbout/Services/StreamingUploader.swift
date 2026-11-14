@preconcurrency import AVFoundation
import Foundation

/// Transcribe while you talk: streams a dictation to the PC while it's recorded
/// (`StreamSession`, Shared/Core/StreamSession.swift), from an AVAudioEngine input tap of its
/// own that turns the microphone into 16 kHz mono 16-bit PCM. Best effort, next to the
/// AVAudioRecorder file, which stays the safe copy: when the tap can't start, the input stops
/// or anything about the stream goes wrong, `finish` returns nil and the dictation goes the
/// usual way with that file.
@MainActor
final class StreamingUploader {
    private let session: StreamSession
    private var capture: StreamCapture?

    private init(session: StreamSession, capture: StreamCapture) {
        self.session = session
        self.capture = capture
    }

    /// Starts the tap and the stream (call it right after the recorder started), or nil when
    /// the tap can't start: then this dictation simply isn't streamed.
    static func start(target: DictationTarget, endpoint: HubEndpoint, hubID: String?) -> StreamingUploader? {
        let inbox = StreamInbox()
        guard let capture = try? StreamCapture(inbox: inbox) else { return nil }
        let session = StreamSession(
            inbox: inbox,
            transport: LiveHubTransport(),
            endpoint: endpoint,
            hubID: hubID,
            target: target,
            isOnWiFi: { await NetworkPath.current().onWiFi }
        )
        Task { await session.begin() }
        return StreamingUploader(session: session, capture: capture)
    }

    /// The tap stops. Before the recorder stops: that closes the audio session, which iOS
    /// doesn't do under a running engine.
    func stopCapture() {
        capture?.stop()
        capture = nil
    }

    /// The rest goes, then the text comes back as the router would have delivered it; nil when
    /// the stream didn't work out.
    func finish(target: DictationTarget, recordedSeconds: TimeInterval) async -> DictationOutcome? {
        stopCapture()
        guard let response = await session.finish(recordedSeconds: recordedSeconds) else { return nil }
        return DictationRouter.streamedOutcome(response, target: target)
    }

    /// The recording was cancelled or thrown away: so is the stream (DELETE).
    func cancel() {
        stopCapture()
        let session = self.session
        Task { await session.cancel() }
    }
}

/// The stream's own input: an AVAudioEngine tap, converted to 16 kHz mono 16-bit PCM for the
/// session's inbox. A route change, an interruption or a media-services reset stops the
/// engine, so the inbox is marked as having lost audio and the stream is given up.
@MainActor
private final class StreamCapture {
    private let engine: AVAudioEngine
    private var observers: [any NSObjectProtocol] = []

    init(inbox: StreamInbox) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0, let converter = StreamConverter(from: format) else {
            throw AudioRecorder.RecorderError.couldNotStart
        }
        // @Sendable: the tap runs on the engine's own thread, never on the main actor.
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { @Sendable (buffer, _) in
            converter.convert(buffer, into: inbox)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw AudioRecorder.RecorderError.couldNotStart
        }
        self.engine = engine
        observers = Self.observeLoss(of: engine, inbox: inbox)
    }

    func stop() {
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
        observers = []
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    private static func observeLoss(of engine: AVAudioEngine, inbox: StreamInbox) -> [any NSObjectProtocol] {
        let center = NotificationCenter.default
        let interruption = center.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { note in
            let type = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard type == AVAudioSession.InterruptionType.began.rawValue else { return }
            inbox.lose()
        }
        let configuration = center.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { _ in
            inbox.lose()
        }
        let reset = center.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil,
            queue: .main
        ) { _ in
            inbox.lose()
        }
        return [interruption, configuration, reset]
    }
}

/// The input's format -> 16 kHz mono Float32 (AVAudioConverter, which keeps its state from
/// buffer to buffer) -> 16-bit PCM (`PCM16`). Used on the tap's thread only.
private final class StreamConverter: @unchecked Sendable {
    private let converter: AVAudioConverter
    private let output: AVAudioFormat
    private let ratio: Double

    init?(from input: AVAudioFormat) {
        guard let output = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Double(StreamAudio.sampleRate),
            channels: 1,
            interleaved: false
        ), let converter = AVAudioConverter(from: input, to: output) else { return nil }
        self.converter = converter
        self.output = output
        ratio = output.sampleRate / input.sampleRate
    }

    func convert(_ buffer: AVAudioPCMBuffer, into inbox: StreamInbox) {
        guard buffer.frameLength > 0 else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let converted = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: capacity) else { return }
        let supply = SupplyOnce()
        var error: NSError?
        let status = converter.convert(to: converted, error: &error) { _, inputStatus in
            guard supply.claim() else {
                inputStatus.pointee = .noDataNow
                return nil
            }
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, error == nil, converted.frameLength > 0, let channels = converted.floatChannelData else {
            return
        }
        let samples = UnsafeBufferPointer<Float>(start: channels[0], count: Int(converted.frameLength))
        inbox.append(PCM16.encode(samples))
    }
}

/// The converter asks for input until it has enough; each tap buffer is given once.
private final class SupplyOnce: @unchecked Sendable {
    private var supplied = false

    func claim() -> Bool {
        guard !supplied else { return false }
        supplied = true
        return true
    }
}

// MARK: - The stream calls over URLSession

extension LiveHubTransport: HubStreamTransport {
    func startStream(target: DictationTarget, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> StreamStarted {
        var request = URLRequest(url: StreamURL.start(endpoint.baseURL))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try? JSONEncoder().encode(StreamStartBody(target: target))
        endpoint.authorize(&request)
        return try await HubClient.sendJSON(request, as: StreamStarted.self)
    }

    func sendChunk(_ audio: Data, seq: Int, streamID: String, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> StreamChunkReply {
        var request = URLRequest(url: StreamURL.audio(endpoint.baseURL, id: streamID, seq: seq))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = audio
        endpoint.authorize(&request)
        let (body, http) = try await HubClient.send(request)
        return try StreamChunkReply.parse(
            status: http.statusCode,
            body: body,
            contentType: http.value(forHTTPHeaderField: "Content-Type")
        ).get()
    }

    func finishStream(streamID: String, chunks: Int, endpoint: HubEndpoint, timeout: TimeInterval) async throws -> TranscribeResponse {
        var request = URLRequest(url: StreamURL.finish(endpoint.baseURL, id: streamID))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try? JSONEncoder().encode(StreamFinishBody(chunks: chunks))
        endpoint.authorize(&request)
        return try await HubClient.sendJSON(request, as: TranscribeResponse.self)
    }

    func cancelStream(streamID: String, endpoint: HubEndpoint) async {
        var request = URLRequest(url: StreamURL.stream(endpoint.baseURL, id: streamID))
        request.httpMethod = "DELETE"
        request.timeoutInterval = 3
        endpoint.authorize(&request)
        _ = try? await HubClient.send(request)
    }
}
