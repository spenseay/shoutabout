import XCTest

/// The "tap your PC, click Allow" pairing (brief: unauthenticated /hello, /pair/request,
/// /pair/status; poll every 1 s for up to 120 s).
final class DevicePairingTests: XCTestCase {
    private let base = Samples.lan

    private func pairing(_ transport: MockTransport, sleeps: Counter = Counter()) -> DevicePairing {
        DevicePairing(transport: transport, sleep: { _ in await sleeps.tick() })
    }

    // MARK: /hello

    func testHelloAcceptsAShoutAboutPC() async throws {
        let hello = try await pairing(MockTransport()).hello(base).get()
        XCTAssertEqual(hello.name, "Sample-Desktop")
    }

    func testHelloRejectsOtherDevicesAndPairingOff() async {
        let printer = await pairing(MockTransport(hello: .success(HelloResponse(app: "printer")))).hello(base)
        XCTAssertEqual(printer.problem?.message, "That address isn't a ShoutAbout PC.")

        var off = HelloResponse.sample
        off.pairing = false
        let closed = await pairing(MockTransport(hello: .success(off))).hello(base)
        XCTAssertTrue(closed.problem?.message.contains("Pairing is turned off on Sample-Desktop") ?? false)

        let away = await pairing(MockTransport(hello: .failure(.unreachable("timed out")))).hello(base)
        XCTAssertTrue(away.problem?.message.contains("same Wi-Fi") ?? false)
    }

    // MARK: /pair/request

    func testRequestSendsTheDeviceNameAndID() async throws {
        let transport = MockTransport()
        let accepted = try await pairing(transport).request(base, deviceName: "  iPhone  ", deviceID: "device-1").get()
        XCTAssertEqual(accepted.code, "4821")
        XCTAssertEqual(accepted.requestID, "req-123")
        let body = await transport.pairBody
        XCTAssertEqual(body?.deviceName, "iPhone")
        XCTAssertEqual(body?.deviceID, "device-1")
    }

    func testRequestProblemsInPlainWords() async {
        let busy = await pairing(MockTransport(pair: .failure(.hub(status: 409, detail: "pending")))).request(base, deviceName: "iPhone", deviceID: "d")
        XCTAssertTrue(busy.problem?.message.contains("already asking") ?? false)

        let old = await pairing(MockTransport(pair: .failure(.hub(status: 404, detail: "Not Found")))).request(base, deviceName: "iPhone", deviceID: "d")
        XCTAssertTrue(old.problem?.message.contains("Update ShoutAbout") ?? false)

        let limited = await pairing(MockTransport(pair: .failure(.hub(status: 429, detail: "slow down")))).request(base, deviceName: "iPhone", deviceID: "d")
        XCTAssertTrue(limited.problem?.message.contains("Wait a minute") ?? false)

        let empty = await pairing(MockTransport(pair: .success(PairRequestAccepted(requestID: "", code: "")))).request(base, deviceName: "iPhone", deviceID: "d")
        XCTAssertNotNil(empty.problem)
    }

    // MARK: /pair/status

    private let hub = PairedHubInfo(id: "hub-1", name: "Sample-Desktop", lanURL: "http://192.168.1.20:8766")

    func testApprovedAfterAFewPolls() async {
        let sleeps = Counter()
        let transport = MockTransport(statuses: [.success(.pending), .success(.pending), .success(.approved(token: "tok", hub: hub))])
        let outcome = await pairing(transport, sleeps: sleeps).waitForApproval(base, requestID: "req-123", expiresIn: 120)
        XCTAssertEqual(outcome, .approved(token: "tok", hub: hub))
        let polls = await transport.calls
        XCTAssertEqual(polls, ["status", "status", "status"])
        let slept = await sleeps.value
        XCTAssertEqual(slept, 3)
    }

    func testDeniedExpiredAndClaimed() async {
        let denied = await pairing(MockTransport(statuses: [.success(.denied)])).waitForApproval(base, requestID: "r", expiresIn: 120)
        XCTAssertEqual(denied, .denied)
        let expired = await pairing(MockTransport(statuses: [.success(.expired)])).waitForApproval(base, requestID: "r", expiresIn: 120)
        XCTAssertEqual(expired, .expired)
        let forgotten = await pairing(MockTransport(statuses: [.failure(.hub(status: 404, detail: "unknown request"))]))
            .waitForApproval(base, requestID: "r", expiresIn: 120)
        XCTAssertEqual(forgotten, .expired)
        let claimed = await pairing(MockTransport(statuses: [.success(.claimed)])).waitForApproval(base, requestID: "r", expiresIn: 120)
        guard case .failed = claimed else { return XCTFail("claimed should fail, got \(claimed)") }
    }

    func testStopsWhenTheRequestExpires() async {
        let transport = MockTransport()  // always pending
        let outcome = await pairing(transport).waitForApproval(base, requestID: "r", expiresIn: 3)
        XCTAssertEqual(outcome, .expired)
        let polls = await transport.calls
        XCTAssertEqual(polls.count, 3)
    }

    func testNeverWaitsLongerThan120Seconds() async {
        let transport = MockTransport()
        let outcome = await pairing(transport).waitForApproval(base, requestID: "r", expiresIn: 600)
        XCTAssertEqual(outcome, .expired)
        let polls = await transport.calls
        XCTAssertEqual(polls.count, 120)
    }

    func testRidesOutAWiFiBlip() async {
        let transport = MockTransport(statuses: [
            .failure(.unreachable("timed out")),
            .failure(.unreachable("timed out")),
            .success(.approved(token: "tok", hub: hub)),
        ])
        let outcome = await pairing(transport).waitForApproval(base, requestID: "r", expiresIn: 120)
        XCTAssertEqual(outcome, .approved(token: "tok", hub: hub))
    }

    func testGivesUpAfterRepeatedErrors() async {
        let transport = MockTransport(statuses: Array(repeating: .failure(.unreachable("timed out")), count: 10))
        let outcome = await pairing(transport).waitForApproval(base, requestID: "r", expiresIn: 120)
        guard case .failed(let message) = outcome else { return XCTFail("expected failed, got \(outcome)") }
        XCTAssertTrue(message.contains("Couldn't reach"), message)
        let polls = await transport.calls
        XCTAssertEqual(polls.count, 5)
    }

    func testCancelledWhileWaiting() async {
        let pairing = DevicePairing(transport: MockTransport(), sleep: { _ in throw CancellationError() })
        let outcome = await pairing.waitForApproval(base, requestID: "r", expiresIn: 120)
        XCTAssertEqual(outcome, .cancelled)
    }
}

private extension Result {
    var problem: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }
}
