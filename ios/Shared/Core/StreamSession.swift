import Foundation

// Transcribe while you talk, the phone side (wire types: StreamProtocol.swift). While a
// dictation is recorded, its audio also goes to the PC in ~2 s chunks, so at "stop" the PC
// only has the last few seconds left to transcribe. Strictly best effort: the recording's own
// .m4a file (AVAudioRecorder) never depends on any of this, and whenever the stream doesn't
// work out, the dictation goes the usual way with that file (DictationRouter).
//
// Everything here is Foundation-only and unit-tested (StreamingTests) with a scripted hub.

/// Float samples (-1...1) as the hub's 16-bit little-endian PCM.
enum PCM16 {
    static func sample(_ value: Float) -> Int16 {
        guard !value.isNaN else { return 0 }
        let clamped = Swift.max(-1, Swift.min(1, value))
        return Int16((clamped * 32767).rounded())
    }

    static func encode<S: Sequence>(_ samples: S) -> Data where S.Element == Float {
        var bytes: [UInt8] = []
        bytes.reserveCapacity(samples.underestimatedCount * 2)
        for value in samples {
            let bits = UInt16(bitPattern: sample(value))
            bytes.append(UInt8(truncatingIfNeeded: bits))
            bytes.append(UInt8(truncatingIfNeeded: bits >> 8))
        }
        return Data(bytes)
    }
}

/// Which dictations are streamed.
enum StreamPolicy {
    /// Where to stream a dictation that's starting, or nil: only to a PC whose cached `/info`
    /// lists "stream" and isn't slow (a slow PC gets text from this iPhone instead, so it never
    /// hears the audio), at its LAN address (Wi-Fi is checked when the stream opens). Never
    /// while the keyboard's session runs: it already has the microphone input of its own.
    static func endpoint(for computer: ComputerLink?, info: HubInfo?, keyboardSession: Bool) -> HubEndpoint? {
        guard !keyboardSession, let info, info.acceptsStream, info.speed != .slow else { return nil }
        return computer?.lanEndpoint
    }
}

/// The stream without the network or a clock: audio in, numbered chunks out in order, what
/// each answer means, and when to give up.
struct StreamUploadState: Equatable, Sendable {
    struct Chunk: Equatable, Sendable {
        var seq: Int
        var data: Data
    }

    /// Tries per chunk after the first; also how many "missing chunk" answers are borne.
    static let maxRetries = 2
    /// Acknowledged chunks kept for a resend after "missing chunk".
    static let keptChunks = 4
    /// More audio than this waiting to go out: the stream has fallen behind and is given up.
    static let maxBacklogBytes = 30 * StreamAudio.bytesPerSecond
    /// The input tap starts a moment after the recorder, so the stream may miss this much of
    /// the recording's start; missing more means it lost audio, and it isn't used.
    static let startSlack: TimeInterval = 0.75

    private(set) var chunkBytes = StreamAudio.chunkBytes
    /// The hub gave a stream id: chunks are cut from now on.
    private(set) var isOpen = false
    private(set) var gaveUp = false
    /// Audio not cut into a chunk yet.
    private(set) var pending = Data()
    /// Cut, not acknowledged yet; the first is the one being sent.
    private(set) var queue: [Chunk] = []
    /// The last few acknowledged chunks.
    private(set) var sent: [Chunk] = []
    /// The next chunk's number, which is also how many chunks were cut.
    private(set) var nextSeq = 0
    private(set) var failures = 0
    private(set) var missingAnswers = 0
    /// All the audio that came in, in bytes.
    private(set) var totalBytes = 0

    init() {}

    mutating func open(maxChunkBytes: Int?) {
        guard !gaveUp, !isOpen else { return }
        if let limit = maxChunkBytes, limit >= 2 {
            chunkBytes = Swift.min(chunkBytes, limit - limit % 2)
        }
        isOpen = true
        cutFullChunks()
    }

    mutating func append(_ audio: Data) {
        guard !gaveUp, !audio.isEmpty else { return }
        pending.append(audio)
        totalBytes += audio.count
        cutFullChunks()
        if backlogBytes > Self.maxBacklogBytes { giveUp() }
    }

    /// Recording stopped: what's left becomes the last chunk.
    mutating func flush() {
        guard isOpen, !gaveUp else { return }
        cutFullChunks()
        let rest = pending.count - pending.count % 2
        if rest > 0 { cutChunk(rest) }
        pending = Data()
    }

    /// The chunk to send now.
    var next: Chunk? { gaveUp ? nil : queue.first }

    /// Everything that came in has been acknowledged.
    var isDrained: Bool { isOpen && !gaveUp && queue.isEmpty && pending.isEmpty }

    var backlogBytes: Int { queue.reduce(pending.count) { $0 + $1.data.count } }

    /// The streamed audio covers a recording this long.
    func covers(recordedSeconds: TimeInterval) -> Bool {
        Double(totalBytes) / Double(StreamAudio.bytesPerSecond) >= recordedSeconds - Self.startSlack
    }

    mutating func handle(_ reply: StreamChunkReply, for chunk: Chunk) {
        guard !gaveUp, queue.first?.seq == chunk.seq else { return }
        switch reply {
        case .accepted(let next):
            // It has everything before `next` (this chunk; after a resend maybe more).
            guard next > chunk.seq, next <= nextSeq else {
                giveUp()
                return
            }
            while let head = queue.first, head.seq < next {
                keep(queue.removeFirst())
            }
            failures = 0
        case .missing(let next):
            missingAnswers += 1
            guard missingAnswers <= Self.maxRetries, next >= 0, next < chunk.seq, requeue(from: next) else {
                giveUp()
                return
            }
            failures = 0
        case .failed(let retryable):
            failures += 1
            if !retryable || failures > Self.maxRetries { giveUp() }
        }
    }

    mutating func giveUp() {
        gaveUp = true
        pending = Data()
        queue = []
        sent = []
    }

    private mutating func cutFullChunks() {
        guard isOpen else { return }
        while pending.count >= chunkBytes {
            cutChunk(chunkBytes)
        }
    }

    private mutating func cutChunk(_ size: Int) {
        queue.append(Chunk(seq: nextSeq, data: Data(pending.prefix(size))))
        pending = Data(pending.dropFirst(size))
        nextSeq += 1
    }

    private mutating func keep(_ chunk: Chunk) {
        sent.append(chunk)
        if sent.count > Self.keptChunks {
            sent.removeFirst(sent.count - Self.keptChunks)
        }
    }

    /// After "missing chunk": the chunks from `seq` on go again, if they're all still kept.
    private mutating func requeue(from seq: Int) -> Bool {
        guard let first = queue.first?.seq, seq >= 0, seq < first else { return false }
        let again = sent.filter { $0.seq >= seq }
        guard again.map(\.seq) == Array(seq..<first) else { return false }
        sent.removeAll { $0.seq >= seq }
        queue.insert(contentsOf: again, at: 0)
        return true
    }
}

/// Audio on its way from the input tap (the audio thread) to the session; thread-safe. After
/// `close` it takes nothing more; `lose` says the input lost audio (it stopped on a route
/// change or an interruption, or nobody read it), which rules the stream out.
final class StreamInbox: @unchecked Sendable {
    /// Unread audio beyond this: the session stopped reading, so the stream is off.
    static let limit = 30 * StreamAudio.bytesPerSecond

    private let lock = NSLock()
    private var audio = Data()
    private var closed = false
    private var lost = false

    init() {}

    func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return }
        audio.append(data)
        if audio.count > Self.limit {
            lost = true
            closed = true
            audio = Data()
        }
    }

    func take() -> Data {
        lock.lock()
        defer { lock.unlock() }
        let taken = audio
        audio = Data()
        return taken
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        closed = true
    }

    func lose() {
        lock.lock()
        defer { lock.unlock() }
        lost = true
        closed = true
        audio = Data()
    }

    var isLost: Bool {
        lock.lock()
        defer { lock.unlock() }
        return lost
    }
}

/// One dictation's stream: Wi-Fi, `/hello` from the paired PC (the token only goes to it),
/// `POST /stream/start`, then the chunks in order, one at a time, as audio comes into `inbox`
/// (`begin`). At stop, `finish` sends the rest and asks for the text, which is the
/// `/transcribe` answer. Anything that goes wrong gives the stream up quietly (DELETE) and
/// `finish` returns nil: the caller then sends the recording's file the usual way. `finish`
/// waits at most `Timing.drain` for the last chunks, so a struggling stream costs a dictation
/// a couple of seconds at worst.
actor StreamSession {
    struct Timing: Sendable {
        var hello: TimeInterval = 1.5
        var start: TimeInterval = 4
        var chunk: TimeInterval = 3
        /// Like the normal upload (HubClient.transcribeTimeout).
        var finish: TimeInterval = 90
        /// The once-more after no answer: the hub keeps a finished result, so it's quick.
        var finishRetry: TimeInterval = 10
        /// After stop, the last chunks get this long.
        var drain: TimeInterval = 3
        var poll: Duration = .milliseconds(100)
        var retryPause: Duration = .milliseconds(250)
    }

    private let inbox: StreamInbox
    private let transport: any HubStreamTransport
    private let endpoint: HubEndpoint
    private let hubID: String?
    private let target: DictationTarget
    private let timing: Timing
    private let isOnWiFi: @Sendable () async -> Bool
    private var state = StreamUploadState()
    private var streamID: String?
    private var pump: Task<Void, Never>?
    /// No more audio is coming (finish or cancel).
    private var stopped = false
    /// Finished or cancelled: `begin` and `finish` do nothing more.
    private var ended = false
    private var deleted = false

    init(
        inbox: StreamInbox,
        transport: any HubStreamTransport,
        endpoint: HubEndpoint,
        hubID: String?,
        target: DictationTarget,
        timing: Timing = Timing(),
        isOnWiFi: @escaping @Sendable () async -> Bool
    ) {
        self.inbox = inbox
        self.transport = transport
        self.endpoint = endpoint
        self.hubID = hubID
        self.target = target
        self.timing = timing
        self.isOnWiFi = isOnWiFi
    }

    /// Opens the stream and sends audio as it comes in, in the background.
    func begin() {
        guard pump == nil, !ended else { return }
        pump = Task { await self.run() }
    }

    /// Recording stopped (the input is already off): the rest goes, then the text comes back.
    /// nil when the stream didn't work out.
    func finish(recordedSeconds: TimeInterval) async -> TranscribeResponse? {
        guard !ended else { return nil }
        inbox.close()
        stopped = true
        let drained = await drain()
        ended = true
        guard drained, !Task.isCancelled, state.isDrained, state.covers(recordedSeconds: recordedSeconds),
              let streamID else {
            abandon()
            return nil
        }
        let chunks = state.nextSeq
        do {
            return try await transport.finishStream(streamID: streamID, chunks: chunks, endpoint: endpoint, timeout: timing.finish)
        } catch {
            // No answer at all (the reply may have been lost): once more, finish is idempotent.
            if case .unreachable = HubFailure.from(error), !Task.isCancelled,
               let response = try? await transport.finishStream(
                   streamID: streamID, chunks: chunks, endpoint: endpoint, timeout: timing.finishRetry
               ) {
                return response
            }
            abandon()
            return nil
        }
    }

    /// The recording was cancelled: the stream goes too.
    func cancel() {
        guard !ended else { return }
        ended = true
        stopped = true
        inbox.close()
        pump?.cancel()
        abandon()
    }

    // MARK: The pump

    private func run() async {
        if await openStream() {
            await sendChunks()
        } else {
            state.giveUp()
        }
        if state.gaveUp { abandon() }
    }

    private func openStream() async -> Bool {
        guard await isOnWiFi(), !ended else { return false }
        guard let hello = try? await transport.hello(endpoint.baseURL, timeout: timing.hello),
              hello.isShoutAbout, !ended else { return false }
        if let hubID, let id = hello.id, id != hubID { return false }
        guard let started = try? await transport.startStream(target: target, endpoint: endpoint, timeout: timing.start),
              StreamStarted.isSafeID(started.streamID) else { return false }
        streamID = started.streamID
        guard !ended else { return false }  // cancelled meanwhile: run() deletes it
        state.open(maxChunkBytes: started.maxChunkBytes)
        return true
    }

    /// In order, one at a time; after `finish`, until everything is acknowledged.
    private func sendChunks() async {
        while !Task.isCancelled {
            state.append(inbox.take())
            if inbox.isLost { state.giveUp() }
            if stopped { state.flush() }
            if state.gaveUp { return }
            guard let chunk = state.next else {
                if stopped { return }
                try? await Task.sleep(for: timing.poll)
                continue
            }
            let reply = await send(chunk)
            state.handle(reply, for: chunk)
            if case .failed = reply, !state.gaveUp {
                try? await Task.sleep(for: timing.retryPause)
            }
        }
        state.giveUp()  // stopped from outside: `finish` waited long enough
    }

    private func send(_ chunk: StreamUploadState.Chunk) async -> StreamChunkReply {
        guard let streamID else { return .failed(retryable: false) }
        do {
            return try await transport.sendChunk(chunk.data, seq: chunk.seq, streamID: streamID, endpoint: endpoint, timeout: timing.chunk)
        } catch {
            return .after(HubFailure.from(error))
        }
    }

    /// Waits for the pump to send the rest, at most `timing.drain`; past that it's stopped.
    private func drain() async -> Bool {
        guard let pump else { return false }
        let limit = timing.drain
        return await withTaskGroup(of: Bool.self) { group in
            group.addTask {
                await pump.value
                return true
            }
            group.addTask {
                try? await Task.sleep(for: .seconds(limit))
                pump.cancel()
                return false
            }
            let first = await group.next() ?? false
            group.cancelAll()
            return first
        }
    }

    /// Gives up: the hub forgets the stream (best effort, in the background).
    private func abandon() {
        state.giveUp()
        inbox.close()
        guard let streamID, !deleted else { return }
        deleted = true
        let transport = self.transport
        let endpoint = self.endpoint
        Task {
            await transport.cancelStream(streamID: streamID, endpoint: endpoint)
        }
    }
}

extension DictationRouter {
    /// A streamed dictation's answer (`/stream/{id}/finish`, the `/transcribe` body), as the
    /// router would have made it from an upload to the same PC on the LAN.
    static func streamedOutcome(_ response: TranscribeResponse, target: DictationTarget) -> DictationOutcome {
        guard target == .pc else {
            // Phone: the PC only wrote the text; it goes on this iPhone's clipboard.
            return DictationOutcome(
                text: response.text,
                rawText: response.rawText,
                delivery: .clipboard,
                route: .lan,
                hubResponse: response
            )
        }
        return outcome(from: response, route: .lan)
    }
}
