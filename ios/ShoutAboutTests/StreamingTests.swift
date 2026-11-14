import XCTest

/// Transcribe while you talk (Shared/Core/StreamProtocol.swift, StreamSession.swift): the wire
/// format, the chunk/retry state machine, and a whole stream against a scripted hub. Whatever
/// goes wrong, `finish` must come back nil quickly (the app then sends the .m4a as before).
final class StreamingTests: XCTestCase {
    private let endpoint = HubEndpoint(baseURL: Samples.lan, route: .lan, token: "device-token")

    /// `seconds` of 16 kHz s16le audio with a recognisable byte pattern.
    private func pcm(seconds: Double, seed: UInt8 = 0) -> Data {
        let count = Int(seconds * Double(StreamAudio.bytesPerSecond))
        return Data((0..<count).map { UInt8(truncatingIfNeeded: $0) &+ seed })
    }

    private func makeSession(
        _ transport: FakeStreamTransport,
        inbox: StreamInbox,
        target: DictationTarget = .pc,
        hubID: String? = "hub-1",
        onWiFi: Bool = true,
        drain: TimeInterval = 2
    ) -> StreamSession {
        StreamSession(
            inbox: inbox,
            transport: transport,
            endpoint: endpoint,
            hubID: hubID,
            target: target,
            timing: StreamSession.Timing(
                hello: 1, start: 1, chunk: 1, finish: 1, finishRetry: 1, drain: drain,
                poll: .milliseconds(5), retryPause: .milliseconds(1)
            ),
            isOnWiFi: { onWiFi }
        )
    }

    /// Waits (up to 2 s) for something that happens in the background, like the DELETE.
    private func eventually(_ condition: () async -> Bool) async -> Bool {
        for _ in 0..<200 {
            if await condition() { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return false
    }

    private func failure<T>(_ result: Result<T, HubFailure>) -> HubFailure? {
        if case .failure(let error) = result { return error }
        return nil
    }

    // MARK: Audio

    func testFloatSamplesBecomeLittleEndianInt16() {
        XCTAssertEqual(PCM16.sample(0), 0)
        XCTAssertEqual(PCM16.sample(1), 32767)
        XCTAssertEqual(PCM16.sample(-1), -32767)
        XCTAssertEqual(PCM16.sample(2), 32767)  // clipped
        XCTAssertEqual(PCM16.sample(-3), -32767)
        XCTAssertEqual(PCM16.sample(.nan), 0)
        XCTAssertEqual(PCM16.sample(0.5), 16384)
        XCTAssertEqual(PCM16.encode([0, 1, -1, 0.5] as [Float]), Data([0x00, 0x00, 0xFF, 0x7F, 0x01, 0x80, 0x00, 0x40]))
        XCTAssertEqual(PCM16.encode([Float]()), Data())
    }

    func testTheFormatIsWhatTheHubTakes() {
        XCTAssertEqual(StreamAudio.sampleRate, 16_000)
        XCTAssertEqual(StreamAudio.encoding, "s16le")
        XCTAssertEqual(StreamAudio.chunkBytes, 64_000)  // ~2 s
        XCTAssertEqual(StreamAudio.chunkBytes % 2, 0)
    }

    // MARK: Wire

    func testStartBody() throws {
        let data = try JSONEncoder().encode(StreamStartBody(target: .phone))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["target"] as? String, "phone")
        XCTAssertEqual(object["sample_rate"] as? Int, 16_000)
        XCTAssertEqual(object["encoding"] as? String, "s16le")
        let finish = try JSONSerialization.jsonObject(with: JSONEncoder().encode(StreamFinishBody(chunks: 3))) as? [String: Any]
        XCTAssertEqual(finish?["chunks"] as? Int, 3)
    }

    func testStartAnswerAndSafeIDs() throws {
        let started = try HubResponseParser.parse(
            StreamStarted.self,
            status: 200,
            body: Data(#"{"stream_id": "Abc_123-xyzAbc_123-xyzAbc_123-xy", "max_chunk_bytes": 262144}"#.utf8),
            contentType: "application/json"
        ).get()
        XCTAssertEqual(started.streamID, "Abc_123-xyzAbc_123-xyzAbc_123-xy")
        XCTAssertEqual(started.maxChunkBytes, 262_144)
        XCTAssertTrue(StreamStarted.isSafeID(started.streamID))
        for unsafe in ["", "../info", "a/b", "a b", "ä", "a?b", String(repeating: "a", count: 129)] {
            XCTAssertFalse(StreamStarted.isSafeID(unsafe), unsafe)
        }
    }

    func testChunkAnswers() {
        let accepted = StreamChunkReply.parse(status: 200, body: Data(#"{"next_seq": 4, "received_s": 8.0}"#.utf8), contentType: "application/json")
        XCTAssertEqual(try accepted.get(), .accepted(nextSeq: 4))
        let missing = StreamChunkReply.parse(status: 409, body: Data(#"{"detail": "missing chunk", "next_seq": 2}"#.utf8), contentType: "application/json")
        XCTAssertEqual(try missing.get(), .missing(nextSeq: 2))
        let paused = StreamChunkReply.parse(status: 409, body: Data(#"{"detail": "paused"}"#.utf8), contentType: "application/json")
        XCTAssertEqual(failure(paused), .hub(status: 409, detail: "paused"))
        let gone = StreamChunkReply.parse(status: 404, body: Data(#"{"detail": "unknown stream"}"#.utf8), contentType: "application/json")
        XCTAssertEqual(failure(gone), .hub(status: 404, detail: "unknown stream"))
    }

    func testWhichChunkFailuresAreRetried() {
        XCTAssertEqual(StreamChunkReply.after(.unreachable("timed out")), .failed(retryable: true))
        XCTAssertEqual(StreamChunkReply.after(.badResponse("not JSON")), .failed(retryable: true))
        XCTAssertEqual(StreamChunkReply.after(.hub(status: 429, detail: "slow down")), .failed(retryable: true))
        XCTAssertEqual(StreamChunkReply.after(.hub(status: 503, detail: "busy")), .failed(retryable: true))
        XCTAssertEqual(StreamChunkReply.after(.gateway(status: 502, snippet: "")), .failed(retryable: true))
        XCTAssertEqual(StreamChunkReply.after(.hub(status: 404, detail: "unknown stream")), .failed(retryable: false))
        XCTAssertEqual(StreamChunkReply.after(.hub(status: 413, detail: "too long")), .failed(retryable: false))
        XCTAssertEqual(StreamChunkReply.after(.hub(status: 401, detail: "token")), .failed(retryable: false))
    }

    func testStreamURLs() {
        XCTAssertEqual(StreamURL.start(Samples.lan).absoluteString, "http://192.168.1.20:8766/stream/start")
        XCTAssertEqual(StreamURL.audio(Samples.lan, id: "abc", seq: 3).absoluteString, "http://192.168.1.20:8766/stream/abc/audio?seq=3")
        XCTAssertEqual(StreamURL.finish(Samples.lan, id: "abc").absoluteString, "http://192.168.1.20:8766/stream/abc/finish")
        XCTAssertEqual(StreamURL.stream(Samples.lan, id: "abc").absoluteString, "http://192.168.1.20:8766/stream/abc")
    }

    // MARK: Which dictations stream

    func testOnlyAPCThatSaysSoOnItsLANAddressGetsAStream() {
        let link = Samples.link()
        XCTAssertTrue(HubInfo.sample(speed: "fast", accepts: ["audio", "text", "stream"]).acceptsStream)
        XCTAssertFalse(HubInfo.sample(speed: "fast").acceptsStream)
        XCTAssertFalse(HubInfo().acceptsStream)

        let streaming = HubInfo.sample(speed: "fast", accepts: ["audio", "text", "stream"])
        XCTAssertEqual(StreamPolicy.endpoint(for: link, info: streaming, keyboardSession: false), link.lanEndpoint)
        XCTAssertEqual(StreamPolicy.endpoint(for: link, info: .sample(speed: "ok", accepts: ["stream"]), keyboardSession: false), link.lanEndpoint)
        // An older PC, no /info yet, a slow PC (this iPhone writes then), the keyboard's session.
        XCTAssertNil(StreamPolicy.endpoint(for: link, info: .sample(speed: "fast"), keyboardSession: false))
        XCTAssertNil(StreamPolicy.endpoint(for: link, info: nil, keyboardSession: false))
        XCTAssertNil(StreamPolicy.endpoint(for: link, info: .sample(speed: "slow", accepts: ["audio", "text", "stream"]), keyboardSession: false))
        XCTAssertNil(StreamPolicy.endpoint(for: link, info: streaming, keyboardSession: true))
        // No computer, or only the tunnel: never.
        XCTAssertNil(StreamPolicy.endpoint(for: nil, info: streaming, keyboardSession: false))
        var tunnelOnly = Samples.link(tunnel: true)
        tunnelOnly.lanURL = nil
        XCTAssertNil(StreamPolicy.endpoint(for: tunnelOnly, info: streaming, keyboardSession: false))
    }

    func testAStreamedAnswerIsDeliveredLikeAnUpload() {
        let pasted = DictationRouter.streamedOutcome(TranscribeResponse(text: Samples.pcText, pasted: true, route: "lan"), target: .pc)
        XCTAssertEqual(pasted.delivery, .pastedOnComputer)
        XCTAssertEqual(pasted.route, .lan)
        XCTAssertNil(pasted.notice)
        XCTAssertNotNil(pasted.hubResponse)

        let notPasted = DictationRouter.streamedOutcome(TranscribeResponse(text: Samples.pcText, pasted: false), target: .pc)
        XCTAssertEqual(notPasted.delivery, .clipboard)
        XCTAssertEqual(notPasted.notice, DictationNotice.notPasted)

        let phone = DictationRouter.streamedOutcome(TranscribeResponse(text: Samples.pcText, pasted: false), target: .phone)
        XCTAssertEqual(phone.delivery, .clipboard)
        XCTAssertEqual(phone.route, .lan)
        XCTAssertNil(phone.notice)
        XCTAssertTrue(phone.needsClipboard)
    }

    // MARK: The state machine

    func testNothingIsCutBeforeTheStreamOpens() {
        var state = StreamUploadState()
        state.append(pcm(seconds: 5))
        XCTAssertNil(state.next)
        XCTAssertEqual(state.pending.count, 160_000)
        state.open(maxChunkBytes: 262_144)
        XCTAssertEqual(state.queue.map(\.seq), [0, 1])
        XCTAssertEqual(state.queue.map(\.data.count), [64_000, 64_000])
        XCTAssertEqual(state.pending.count, 32_000)
        XCTAssertEqual(state.next?.seq, 0)
        XCTAssertEqual(state.totalBytes, 160_000)
    }

    func testTheHubsChunkLimitIsKept() {
        var state = StreamUploadState()
        state.open(maxChunkBytes: 10_001)
        XCTAssertEqual(state.chunkBytes, 10_000)  // even
        state.append(pcm(seconds: 1))
        XCTAssertEqual(state.queue.count, 3)
        XCTAssertEqual(state.pending.count, 2_000)
    }

    func testTheRestIsTheLastChunk() {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(pcm(seconds: 2.5))
        state.append(Data([1]))  // an odd byte can't be a sample
        state.flush()
        XCTAssertEqual(state.queue.map(\.data.count), [64_000, 16_000])
        XCTAssertEqual(state.nextSeq, 2)
        XCTAssertTrue(state.pending.isEmpty)
    }

    func testAcknowledgedChunksAreDoneAndKeptForAResend() throws {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(pcm(seconds: 6))
        let first = try XCTUnwrap(state.next)
        state.handle(.accepted(nextSeq: 1), for: first)
        XCTAssertEqual(state.next?.seq, 1)
        let second = try XCTUnwrap(state.next)
        state.handle(.accepted(nextSeq: 2), for: second)
        XCTAssertEqual(state.sent.map(\.seq), [0, 1])

        // The hub lost chunk 1: it goes again, then 2.
        let third = try XCTUnwrap(state.next)
        XCTAssertEqual(third.seq, 2)
        state.handle(.missing(nextSeq: 1), for: third)
        XCTAssertFalse(state.gaveUp)
        XCTAssertEqual(state.queue.map(\.seq), [1, 2])
        let again = try XCTUnwrap(state.next)
        state.handle(.accepted(nextSeq: 3), for: again)  // it had 2 as well
        XCTAssertTrue(state.queue.isEmpty)
        XCTAssertTrue(state.isDrained)
    }

    func testAMissingChunkThatIsNoLongerKeptGivesUp() throws {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(pcm(seconds: 14))  // 7 chunks
        for seq in 0..<6 {
            let chunk = try XCTUnwrap(state.next)
            state.handle(.accepted(nextSeq: seq + 1), for: chunk)
        }
        XCTAssertEqual(state.sent.count, StreamUploadState.keptChunks)
        let last = try XCTUnwrap(state.next)
        state.handle(.missing(nextSeq: 0), for: last)
        XCTAssertTrue(state.gaveUp)
        XCTAssertNil(state.next)
    }

    func testAFailedChunkIsTriedTwiceMoreThenGivenUp() throws {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(pcm(seconds: 2))
        let chunk = try XCTUnwrap(state.next)
        state.handle(.failed(retryable: true), for: chunk)
        state.handle(.failed(retryable: true), for: chunk)
        XCTAssertFalse(state.gaveUp)
        XCTAssertEqual(state.next, chunk)  // the same one again
        state.handle(.failed(retryable: true), for: chunk)
        XCTAssertTrue(state.gaveUp)
        XCTAssertNil(state.next)
        XCTAssertFalse(state.isDrained)
    }

    func testARetryThatWorksStartsTheCountAgain() throws {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(pcm(seconds: 4))
        let first = try XCTUnwrap(state.next)
        state.handle(.failed(retryable: true), for: first)
        state.handle(.failed(retryable: true), for: first)
        state.handle(.accepted(nextSeq: 1), for: first)
        let second = try XCTUnwrap(state.next)
        state.handle(.failed(retryable: true), for: second)
        state.handle(.failed(retryable: true), for: second)
        XCTAssertFalse(state.gaveUp)
    }

    func testHopelessAnswersGiveUpAtOnce() throws {
        for reply in [StreamChunkReply.failed(retryable: false), .accepted(nextSeq: 0), .accepted(nextSeq: 5), .missing(nextSeq: 0)] {
            var state = StreamUploadState()
            state.open(maxChunkBytes: nil)
            state.append(pcm(seconds: 2))
            let chunk = try XCTUnwrap(state.next)
            state.handle(reply, for: chunk)
            XCTAssertTrue(state.gaveUp, "\(reply)")
        }
    }

    func testAnAnswerForAnotherChunkIsIgnored() throws {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(pcm(seconds: 4))
        let stale = StreamUploadState.Chunk(seq: 7, data: Data())
        state.handle(.failed(retryable: false), for: stale)
        XCTAssertFalse(state.gaveUp)
        XCTAssertEqual(state.next?.seq, 0)
    }

    func testFallingFarBehindGivesUp() {
        var state = StreamUploadState()
        state.open(maxChunkBytes: nil)
        state.append(Data(count: StreamUploadState.maxBacklogBytes))
        XCTAssertFalse(state.gaveUp)
        state.append(Data(count: 2))
        XCTAssertTrue(state.gaveUp)
        XCTAssertEqual(state.backlogBytes, 0)
    }

    func testTheStreamMustCoverTheRecording() {
        var state = StreamUploadState()
        state.append(pcm(seconds: 5))
        XCTAssertTrue(state.covers(recordedSeconds: 5))
        XCTAssertTrue(state.covers(recordedSeconds: 5 + StreamUploadState.startSlack))
        XCTAssertFalse(state.covers(recordedSeconds: 6))
    }

    func testTheInboxHandsOverOnceAndStopsWhenClosedOrLost() {
        let inbox = StreamInbox()
        inbox.append(Data([1, 2]))
        inbox.append(Data([3, 4]))
        XCTAssertEqual(inbox.take(), Data([1, 2, 3, 4]))
        XCTAssertEqual(inbox.take(), Data())
        inbox.close()
        inbox.append(Data([5, 6]))
        XCTAssertEqual(inbox.take(), Data())
        XCTAssertFalse(inbox.isLost)
        inbox.lose()
        XCTAssertTrue(inbox.isLost)

        let flooded = StreamInbox()
        flooded.append(Data(count: StreamInbox.limit + 2))
        XCTAssertTrue(flooded.isLost)
        XCTAssertEqual(flooded.take(), Data())
    }

    // MARK: A whole stream

    func testChunksGoInOrderThenFinishReturnsTheText() async {
        let transport = FakeStreamTransport()
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        let first = pcm(seconds: 5, seed: 1)
        let second = pcm(seconds: 1, seed: 2)
        inbox.append(first)
        await session.begin()
        inbox.append(second)
        let response = await session.finish(recordedSeconds: 6)
        XCTAssertEqual(response?.text, Samples.pcText)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "start:pc", "audio:0", "audio:1", "audio:2", "finish:3"])
        let received = await transport.received
        XCTAssertEqual(received, first + second)
        let tokens = await transport.tokensSent
        XCTAssertEqual(tokens, ["device-token"])
    }

    func testTheLastFewSecondsGoAsAShorterChunk() async {
        let transport = FakeStreamTransport()
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox, target: .phone)
        inbox.append(pcm(seconds: 2.5))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2.5)
        XCTAssertNotNil(response)
        let sizes = await transport.chunkSizes
        XCTAssertEqual(sizes, [64_000, 16_000])
        let calls = await transport.calls
        XCTAssertEqual(calls.first(where: { $0.hasPrefix("start") }), "start:phone")
        XCTAssertEqual(calls.last, "finish:2")
    }

    func testOffWiFiNothingIsSent() async {
        let transport = FakeStreamTransport()
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox, onWiFi: false)
        inbox.append(pcm(seconds: 1))
        await session.begin()
        let response = await session.finish(recordedSeconds: 1)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls, [])
    }

    func testTheTokenOnlyGoesToThePairedPC() async {
        let stranger = HelloResponse(app: "shoutabout", v: 1, id: "someone-else", name: "Other-PC", version: "2.0.0", pairing: true)
        let transport = FakeStreamTransport(hello: .success(stranger))
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 1))
        await session.begin()
        let response = await session.finish(recordedSeconds: 1)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello"])
        let tokens = await transport.tokensSent
        XCTAssertEqual(tokens, [])
    }

    func testAPCNotReadyMeansTheUsualWay() async {
        let transport = FakeStreamTransport(start: .failure(.hub(status: 503, detail: "model loading")))
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 1))
        await session.begin()
        let response = await session.finish(recordedSeconds: 1)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "start:pc"])
    }

    func testAnUnsafeStreamIDIsNeverUsed() async {
        let transport = FakeStreamTransport(start: .success(StreamStarted(streamID: "../info", maxChunkBytes: nil)))
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 1))
        await session.begin()
        let response = await session.finish(recordedSeconds: 1)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "start:pc"])
    }

    func testAFailedChunkIsSentAgain() async {
        let transport = FakeStreamTransport(chunkReplies: [.failure(.unreachable("connection lost"))])
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNotNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "start:pc", "audio:0", "audio:0", "finish:1"])
    }

    func testAChunkThatKeepsFailingGivesTheStreamUp() async {
        let lost = Result<StreamChunkReply, HubFailure>.failure(.unreachable("timed out"))
        let transport = FakeStreamTransport(chunkReplies: [lost, lost, lost])
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNil(response)
        let deleted = await eventually { await transport.calls.contains("delete") }
        XCTAssertTrue(deleted)
        let calls = await transport.calls
        XCTAssertEqual(calls.filter { $0 == "audio:0" }.count, 3)
        XCTAssertFalse(calls.contains { $0.hasPrefix("finish") })
    }

    func testAGoneStreamGivesUpAtOnce() async {
        let transport = FakeStreamTransport(chunkReplies: [.failure(.hub(status: 404, detail: "unknown stream"))])
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls.filter { $0 == "audio:0" }.count, 1)
    }

    func testFinishWithoutAnAnswerIsAskedOnceMore() async {
        let transport = FakeStreamTransport(finish: [.failure(.unreachable("connection lost"))])
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertEqual(response?.text, Samples.pcText)
        let calls = await transport.calls
        XCTAssertEqual(calls.filter { $0 == "finish:1" }.count, 2)
        XCTAssertFalse(calls.contains("delete"))
    }

    func testFinishThatFailsTwiceMeansTheUsualWay() async {
        let lost = Result<TranscribeResponse, HubFailure>.failure(.unreachable("timed out"))
        let transport = FakeStreamTransport(finish: [lost, lost])
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertEqual(calls.filter { $0 == "finish:1" }.count, 2)
    }

    func testFinishTheHubRefusesIsNotRetried() async {
        let transport = FakeStreamTransport(finish: [.failure(.hub(status: 409, detail: "missing chunks"))])
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNil(response)
        let deleted = await eventually { await transport.calls.contains("delete") }
        XCTAssertTrue(deleted)
        let calls = await transport.calls
        XCTAssertEqual(calls.filter { $0.hasPrefix("finish") }.count, 1)
    }

    func testLostAudioIsNeverUsed() async {
        let transport = FakeStreamTransport()
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        inbox.lose()  // a route change stopped the input
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNil(response)
        let calls = await transport.calls
        XCTAssertFalse(calls.contains { $0.hasPrefix("finish") })
    }

    func testAStreamShorterThanTheRecordingIsNeverUsed() async {
        let transport = FakeStreamTransport()
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let response = await session.finish(recordedSeconds: 10)
        XCTAssertNil(response)
        let deleted = await eventually { await transport.calls.contains("delete") }
        XCTAssertTrue(deleted)
        let calls = await transport.calls
        XCTAssertFalse(calls.contains { $0.hasPrefix("finish") })
    }

    func testAStuckStreamCostsAtMostTheDrainTime() async {
        let transport = FakeStreamTransport(chunkDelay: .seconds(5))
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox, drain: 0.2)
        inbox.append(pcm(seconds: 2))
        await session.begin()
        let started = ContinuousClock.now
        let response = await session.finish(recordedSeconds: 2)
        XCTAssertNil(response)
        XCTAssertLessThan(ContinuousClock.now - started, .seconds(2))
    }

    func testCancelDeletesTheStream() async {
        let transport = FakeStreamTransport()
        let inbox = StreamInbox()
        let session = makeSession(transport, inbox: inbox)
        inbox.append(pcm(seconds: 1))
        await session.begin()
        let opened = await eventually { await transport.calls.contains("start:pc") }
        XCTAssertTrue(opened)
        await session.cancel()
        let deleted = await eventually { await transport.calls.contains("delete") }
        XCTAssertTrue(deleted)
        let response = await session.finish(recordedSeconds: 1)
        XCTAssertNil(response)
    }
}
