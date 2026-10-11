import XCTest

/// The v2 phone routing (brief: "Phone routing (iPhone implements)").
final class DictationRouterTests: XCTestCase {
    private func run(
        _ request: DictationRequest,
        transport: MockTransport = MockTransport(),
        onDevice: MockOnDevice = MockOnDevice(),
        log: StageLog? = nil
    ) async -> Result<DictationOutcome, DictationFailure> {
        let router = DictationRouter(transport: transport, onDevice: onDevice)
        return await router.run(request) { stage in
            await log?.add(stage)
        }
    }

    // MARK: Phone target (always the clipboard; a fast PC at home writes the text)

    func testPhoneTargetWithoutAComputerTranscribesHere() async throws {
        let transport = MockTransport()
        let onDevice = MockOnDevice()
        let outcome = try await run(Samples.request(target: .phone, computer: nil), transport: transport, onDevice: onDevice).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertEqual(outcome.route, .offline)
        XCTAssertNil(outcome.notice)
        XCTAssertTrue(outcome.needsClipboard)
        XCTAssertEqual(outcome.engine, "SpeechTranscriber")
        let calls = await transport.calls
        XCTAssertEqual(calls, [])
        let count = await onDevice.count
        XCTAssertEqual(count, 1)
    }

    func testPhoneTargetLetsAFastPCWriteTheText() async throws {
        let transport = MockTransport(info: .success(.sample(speed: "fast")))
        let onDevice = MockOnDevice()
        let log = StageLog()
        let outcome = try await run(Samples.request(target: .phone), transport: transport, onDevice: onDevice, log: log).get()
        XCTAssertEqual(outcome.text, Samples.pcText)
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertEqual(outcome.route, .lan)
        XCTAssertNil(outcome.notice)
        XCTAssertNil(outcome.engine)
        XCTAssertTrue(outcome.needsClipboard)
        XCTAssertEqual(outcome.learnedInfo?.speed, .fast)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "info", "transcribe:lan"])
        let targets = await transport.targetsSent
        XCTAssertEqual(targets, [.phone])
        let count = await onDevice.count
        XCTAssertEqual(count, 0)
        let stages = await log.stages
        XCTAssertEqual(stages, [.lookingForComputer, .sendingAudio(.lan)])
    }

    func testPhoneTargetUsesAnOlderPCWithoutInfo() async throws {
        let transport = MockTransport(
            hello: .failure(.hub(status: 404, detail: "Not Found")),
            info: .failure(.hub(status: 404, detail: "Not Found"))
        )
        let outcome = try await run(Samples.request(target: .phone, computer: Samples.link(hubID: nil)), transport: transport).get()
        XCTAssertEqual(outcome.text, Samples.pcText)
        XCTAssertEqual(outcome.route, .lan)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "health", "info", "transcribe:lan"])
        let targets = await transport.targetsSent
        XCTAssertEqual(targets, [.phone])
    }

    func testPhoneTargetWithASlowPCTranscribesHere() async throws {
        let transport = MockTransport()
        let onDevice = MockOnDevice()
        let outcome = try await run(
            Samples.request(target: .phone, cachedInfo: .sample(speed: "slow")),
            transport: transport, onDevice: onDevice
        ).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.route, .offline)
        XCTAssertNil(outcome.notice)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello"])
        let count = await onDevice.count
        XCTAssertEqual(count, 1)
    }

    func testPhoneTargetAwayFromHomeTranscribesHereWithoutANote() async throws {
        let transport = MockTransport(hello: .failure(.unreachable("timed out")))
        let outcome = try await run(Samples.request(target: .phone, computer: Samples.link(tunnel: true)), transport: transport).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.route, .offline)
        XCTAssertNil(outcome.notice)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello"], "the tunnel is never used for the Phone target")
    }

    func testPhoneTargetOnCellularDoesNotKnock() async throws {
        let transport = MockTransport()
        let outcome = try await run(Samples.request(target: .phone, onWiFi: false), transport: transport).get()
        XCTAssertEqual(outcome.route, .offline)
        let calls = await transport.calls
        XCTAssertEqual(calls, [])
    }

    func testPhoneTargetFallsBackQuietlyOnAnyPCError() async throws {
        let transport = MockTransport(lanTranscribe: .failure(.hub(status: 503, detail: "hub is starting")))
        let onDevice = MockOnDevice()
        let outcome = try await run(Samples.request(target: .phone), transport: transport, onDevice: onDevice).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertNil(outcome.notice)
        XCTAssertEqual(outcome.learnedInfo?.speed, .fast)
        let count = await onDevice.count
        XCTAssertEqual(count, 1)
    }

    func testPhoneTargetNeverTrustsADifferentPC() async throws {
        var other = HelloResponse.sample
        other.id = "someone-else"
        let transport = MockTransport(hello: .success(other))
        let outcome = try await run(Samples.request(target: .phone), transport: transport).get()
        XCTAssertEqual(outcome.route, .offline)
        let tokens = await transport.tokensSent
        XCTAssertEqual(tokens, [])
    }

    func testOnDeviceFailureIsAFailure() async {
        let result = await run(Samples.request(target: .phone, computer: nil), onDevice: MockOnDevice(.failure(TestError())))
        guard case .failure(let failure) = result else { return XCTFail("expected a failure") }
        XCTAssertTrue(failure.message.contains("couldn't write it down"), failure.message)
        XCTAssertTrue(failure.message.contains("isn't downloaded"), failure.message)
    }

    func testEmptyTextHasNoNotice() async throws {
        let onDevice = MockOnDevice(.success(OnDeviceResult(text: "", engine: "SpeechTranscriber")))
        let outcome = try await run(Samples.request(target: .pc, computer: nil), onDevice: onDevice).get()
        XCTAssertEqual(outcome.text, "")
        XCTAssertNil(outcome.notice)
        XCTAssertFalse(outcome.needsClipboard)
    }

    // MARK: Computer target, PC at home

    func testFastComputerGetsTheAudioAndPastes() async throws {
        let transport = MockTransport(info: .success(.sample(speed: "fast")))
        let onDevice = MockOnDevice()
        let log = StageLog()
        let outcome = try await run(Samples.request(), transport: transport, onDevice: onDevice, log: log).get()
        XCTAssertEqual(outcome.text, Samples.pcText)
        XCTAssertEqual(outcome.delivery, .pastedOnComputer)
        XCTAssertEqual(outcome.route, .lan)
        XCTAssertNil(outcome.notice)
        XCTAssertFalse(outcome.needsClipboard)
        XCTAssertEqual(outcome.learnedInfo?.speed, .fast)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "info", "transcribe:lan"])
        let tokens = await transport.tokensSent
        XCTAssertEqual(Set(tokens), ["device-token"])
        let targets = await transport.targetsSent
        XCTAssertEqual(targets, [.pc])
        let count = await onDevice.count
        XCTAssertEqual(count, 0)
        let stages = await log.stages
        XCTAssertEqual(stages, [.lookingForComputer, .sendingAudio(.lan)])
    }

    func testCachedInfoSkipsTheInfoCall() async throws {
        let transport = MockTransport()
        let outcome = try await run(Samples.request(cachedInfo: .sample(speed: "ok")), transport: transport).get()
        XCTAssertEqual(outcome.delivery, .pastedOnComputer)
        XCTAssertNil(outcome.learnedInfo)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "transcribe:lan"])
    }

    func testSlowComputerGetsTextWrittenByThePhone() async throws {
        let transport = MockTransport()
        let onDevice = MockOnDevice()
        let log = StageLog()
        let outcome = try await run(
            Samples.request(cachedInfo: .sample(speed: "slow")),
            transport: transport, onDevice: onDevice, log: log
        ).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.delivery, .pastedOnComputer)
        XCTAssertEqual(outcome.route, .lan)
        XCTAssertEqual(outcome.engine, "SpeechTranscriber")
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "paste"])
        let pasted = await transport.pastedText
        XCTAssertEqual(pasted, Samples.phoneText)
        let stages = await log.stages
        XCTAssertEqual(stages, [.lookingForComputer, .transcribingOnPhone, .pastingOnComputer])
    }

    func testSlowComputerWithoutTextSupportStillGetsAudio() async throws {
        let transport = MockTransport(info: .success(.sample(speed: "slow", accepts: ["audio"])))
        let outcome = try await run(Samples.request(), transport: transport).get()
        XCTAssertEqual(outcome.text, Samples.pcText)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "info", "transcribe:lan"])
    }

    func testPausedComputerKeepsTheTextOnTheClipboard() async throws {
        let transport = MockTransport(paste: .failure(.hub(status: 409, detail: "paused")))
        let outcome = try await run(Samples.request(cachedInfo: .sample(speed: "slow")), transport: transport).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertEqual(outcome.notice, DictationNotice.paused)
        XCTAssertTrue(outcome.needsClipboard)
    }

    func testTooLongToPasteGoesToTheClipboard() async throws {
        let long = String(repeating: "a", count: PasteBody.maxLength + 1)
        let transport = MockTransport()
        let onDevice = MockOnDevice(.success(OnDeviceResult(text: long, engine: "SpeechTranscriber")))
        let outcome = try await run(Samples.request(cachedInfo: .sample(speed: "slow")), transport: transport, onDevice: onDevice).get()
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertEqual(outcome.notice, DictationNotice.tooLong)
        let calls = await transport.calls
        XCTAssertFalse(calls.contains("paste"))
    }

    func testPCThatDidNotPasteMeansClipboard() async throws {
        let transport = MockTransport(lanTranscribe: .success(TranscribeResponse(text: Samples.pcText, pasted: false)))
        let outcome = try await run(Samples.request(), transport: transport).get()
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertEqual(outcome.notice, DictationNotice.notPasted)
        XCTAssertTrue(outcome.needsClipboard)
    }

    // MARK: Older PCs

    func testOlderPCWithoutHelloOrInfoStillGetsTheAudio() async throws {
        let transport = MockTransport(
            hello: .failure(.hub(status: 404, detail: "Not Found")),
            info: .failure(.hub(status: 404, detail: "Not Found"))
        )
        let outcome = try await run(Samples.request(computer: Samples.link(hubID: nil)), transport: transport).get()
        XCTAssertEqual(outcome.delivery, .pastedOnComputer)
        XCTAssertNil(outcome.learnedInfo)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "health", "info", "transcribe:lan"])
    }

    // MARK: Away / problems

    func testUnreachablePCMeansPhoneAndClipboardWithANote() async throws {
        let transport = MockTransport(hello: .failure(.unreachable("timed out")))
        let onDevice = MockOnDevice()
        let outcome = try await run(Samples.request(), transport: transport, onDevice: onDevice).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.delivery, .clipboard)
        XCTAssertEqual(outcome.route, .offline)
        XCTAssertEqual(outcome.notice, "Your PC isn't reachable.")
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello"])
    }

    func testADifferentPCAtTheSameAddressNeverGetsTheToken() async throws {
        var other = HelloResponse.sample
        other.id = "someone-else"
        let transport = MockTransport(hello: .success(other))
        let outcome = try await run(Samples.request(), transport: transport).get()
        XCTAssertEqual(outcome.notice, DictationNotice.away)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello"])
        let tokens = await transport.tokensSent
        XCTAssertEqual(tokens, [])
    }

    func testSomethingElseAnsweringHelloIsNotAPC() async throws {
        let transport = MockTransport(hello: .success(HelloResponse(app: "printer")))
        let outcome = try await run(Samples.request(), transport: transport).get()
        XCTAssertEqual(outcome.notice, DictationNotice.away)
        let tokens = await transport.tokensSent
        XCTAssertEqual(tokens, [])
    }

    func testCellularSkipsTheHomeCheck() async throws {
        let transport = MockTransport()
        let outcome = try await run(Samples.request(onWiFi: false), transport: transport).get()
        XCTAssertEqual(outcome.notice, DictationNotice.away)
        let calls = await transport.calls
        XCTAssertEqual(calls, [])
    }

    func testVPNAddressIsStillTriedOnCellular() async throws {
        let transport = MockTransport()
        let link = Samples.link(lan: URL(string: "http://100.100.1.2:8766")!)
        let outcome = try await run(Samples.request(computer: link, onWiFi: false), transport: transport).get()
        XCTAssertEqual(outcome.delivery, .pastedOnComputer)
    }

    func testBusyPCFallsBackToThePhone() async throws {
        let transport = MockTransport(lanTranscribe: .failure(.hub(status: 503, detail: "hub is starting")))
        let onDevice = MockOnDevice()
        let outcome = try await run(Samples.request(), transport: transport, onDevice: onDevice).get()
        XCTAssertEqual(outcome.text, Samples.phoneText)
        XCTAssertEqual(outcome.notice, DictationNotice.busy)
        XCTAssertEqual(outcome.learnedInfo?.speed, .fast)
        let count = await onDevice.count
        XCTAssertEqual(count, 1)
    }

    func testRejectedTokenStillDeliversWithAReconnectNote() async throws {
        let transport = MockTransport(lanTranscribe: .failure(.hub(status: 401, detail: "missing or invalid bearer token")))
        let outcome = try await run(Samples.request(), transport: transport).get()
        XCTAssertEqual(outcome.notice, DictationNotice.rejected)
        XCTAssertEqual(outcome.delivery, .clipboard)
    }

    func testNoComputerCopiesWithANote() async throws {
        let outcome = try await run(Samples.request(computer: nil)).get()
        XCTAssertEqual(outcome.notice, DictationNotice.noComputer)
        XCTAssertEqual(outcome.delivery, .clipboard)
    }

    func testAwayAndOnDeviceFailureSaysBoth() async {
        let transport = MockTransport(hello: .failure(.unreachable("timed out")))
        let result = await run(Samples.request(), transport: transport, onDevice: MockOnDevice(.failure(TestError())))
        guard case .failure(let failure) = result else { return XCTFail("expected a failure") }
        XCTAssertTrue(failure.message.hasPrefix("Your PC isn't reachable"), failure.message)
    }

    // MARK: Tunnel (advanced, legacy pairing)

    func testTunnelWhenThePCIsNotOnTheLAN() async throws {
        let transport = MockTransport(hello: .failure(.unreachable("timed out")))
        let outcome = try await run(Samples.request(computer: Samples.link(tunnel: true)), transport: transport).get()
        XCTAssertEqual(outcome.route, .tunnel)
        XCTAssertEqual(outcome.delivery, .pastedOnComputer)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "transcribe:tunnel"])
        let tokens = await transport.tokensSent
        XCTAssertEqual(tokens, ["legacy-token"])
    }

    func testUploadDroppedMidwayTriesTheTunnelThenThePhone() async throws {
        let transport = MockTransport(
            lanTranscribe: .failure(.unreachable("connection lost")),
            tunnelTranscribe: .failure(.gateway(status: 530, snippet: "<html>"))
        )
        let onDevice = MockOnDevice()
        let outcome = try await run(Samples.request(computer: Samples.link(tunnel: true)), transport: transport, onDevice: onDevice).get()
        XCTAssertEqual(outcome.notice, DictationNotice.away)
        XCTAssertEqual(outcome.route, .offline)
        let calls = await transport.calls
        XCTAssertEqual(calls, ["hello", "info", "transcribe:lan", "transcribe:tunnel"])
    }

    // MARK: Notices

    func testNoticeForEachFailure() {
        XCTAssertEqual(DictationNotice.forFailure(.unreachable("x")), DictationNotice.away)
        XCTAssertEqual(DictationNotice.forFailure(.gateway(status: 502, snippet: "")), DictationNotice.away)
        XCTAssertEqual(DictationNotice.forFailure(.hub(status: 401, detail: "")), DictationNotice.rejected)
        XCTAssertEqual(DictationNotice.forFailure(.hub(status: 409, detail: "")), DictationNotice.paused)
        XCTAssertEqual(DictationNotice.forFailure(.hub(status: 413, detail: "")), DictationNotice.tooLong)
        XCTAssertEqual(DictationNotice.forFailure(.hub(status: 503, detail: "")), DictationNotice.busy)
        XCTAssertEqual(DictationNotice.forFailure(.badResponse("x")), DictationNotice.busy)
    }
}
