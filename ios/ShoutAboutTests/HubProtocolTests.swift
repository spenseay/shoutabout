import XCTest

/// JSON shapes of the v2 cross-device contract.
final class HubProtocolTests: XCTestCase {
    private func data(_ text: String) -> Data { Data(text.utf8) }

    private func json(_ value: some Encodable) throws -> [String: Any] {
        let encoded = try JSONEncoder().encode(value)
        let object = try JSONSerialization.jsonObject(with: encoded)
        return try XCTUnwrap(object as? [String: Any])
    }

    func testHello() throws {
        let hello = try JSONDecoder().decode(HelloResponse.self, from: data(
            #"{"app":"shoutabout","v":1,"id":"6f1c0000-aaaa","name":"DESKTOP-SAMPLE","version":"2.0.0","pairing":true}"#
        ))
        XCTAssertTrue(hello.isShoutAbout)
        XCTAssertEqual(hello.id, "6f1c0000-aaaa")
        XCTAssertEqual(hello.name, "DESKTOP-SAMPLE")
        XCTAssertEqual(hello.pairing, true)
        let minimal = try JSONDecoder().decode(HelloResponse.self, from: data(#"{"app":"other","extra":42}"#))
        XCTAssertFalse(minimal.isShoutAbout)
    }

    func testPairRequestBody() throws {
        let body = try json(PairRequestBody(deviceName: "iPhone", deviceID: "abc"))
        XCTAssertEqual(body["device_name"] as? String, "iPhone")
        XCTAssertEqual(body["device_id"] as? String, "abc")
        XCTAssertEqual(body.count, 2)

        let long = PairRequestBody(deviceName: String(repeating: "x", count: 80), deviceID: "abc")
        XCTAssertEqual(long.deviceName.count, 60)
        XCTAssertEqual(PairRequestBody(deviceName: "   ", deviceID: "abc").deviceName, "iPhone")
    }

    func testPairRequestAccepted() throws {
        let accepted = try JSONDecoder().decode(PairRequestAccepted.self, from: data(
            #"{"request_id":"kq3-Xz_9","code":"4821","expires_in":120}"#
        ))
        XCTAssertEqual(accepted, PairRequestAccepted(requestID: "kq3-Xz_9", code: "4821", expiresIn: 120))
    }

    func testPairStatuses() throws {
        XCTAssertEqual(try PairStatus.decode(data(#"{"status":"pending"}"#)), .pending)
        XCTAssertEqual(try PairStatus.decode(data(#"{"status":"denied"}"#)), .denied)
        XCTAssertEqual(try PairStatus.decode(data(#"{"status":"expired"}"#)), .expired)
        XCTAssertEqual(try PairStatus.decode(data(#"{"status":"claimed"}"#)), .claimed)
        XCTAssertEqual(
            try PairStatus.decode(data(
                #"{"status":"approved","token":"tok-123","hub":{"id":"hub-1","name":"Sample-Desktop","lan_url":"http://192.168.1.20:8766"}}"#
            )),
            .approved(token: "tok-123", hub: PairedHubInfo(id: "hub-1", name: "Sample-Desktop", lanURL: "http://192.168.1.20:8766"))
        )
        XCTAssertEqual(try PairStatus.decode(data(#"{"status":"approved"}"#)), .unknown("approved without a token"))
        XCTAssertEqual(try PairStatus.decode(data(#"{"status":"teleported"}"#)), .unknown("teleported"))
        XCTAssertThrowsError(try PairStatus.decode(data("<html>")))
    }

    func testInfoSpeed() throws {
        let slow = try JSONDecoder().decode(HubInfo.self, from: data(
            #"{"id":"hub-1","name":"Sample-Desktop","version":"2.0.0","stt":{"tier":"light","device":"cpu","speed":"slow"},"cleanup":false,"accepts":["audio","text"]}"#
        ))
        XCTAssertEqual(slow.speed, .slow)
        XCTAssertTrue(slow.acceptsText)
        XCTAssertTrue(slow.prefersText)

        let fast = try JSONDecoder().decode(HubInfo.self, from: data(
            #"{"id":"hub-1","stt":{"tier":"best","device":"cuda","speed":"fast"},"cleanup":true,"accepts":["audio","text"]}"#
        ))
        XCTAssertEqual(fast.speed, .fast)
        XCTAssertFalse(fast.prefersText)

        let bare = try JSONDecoder().decode(HubInfo.self, from: data("{}"))
        XCTAssertEqual(bare.speed, .ok)
        XCTAssertFalse(bare.acceptsText)

        let odd = try JSONDecoder().decode(HubInfo.self, from: data(#"{"stt":{"speed":"warp"},"accepts":["text"]}"#))
        XCTAssertEqual(odd.speed, .ok)
        XCTAssertFalse(odd.prefersText)
    }

    func testPasteBodyAndResponse() throws {
        XCTAssertEqual(try json(PasteBody(text: "Hello.")) as NSDictionary, ["text": "Hello."] as NSDictionary)
        let response = try JSONDecoder().decode(PasteResponse.self, from: data(#"{"status":"ok","pasted":true}"#))
        XCTAssertEqual(response.pasted, true)
    }

    func testCachedInfoFreshness() {
        let now = Date()
        let cached = CachedHubInfo(info: .sample(speed: "slow"), fetchedAt: now.addingTimeInterval(-3600), hubKey: "hub-1")
        XCTAssertTrue(cached.isFresh(for: "hub-1", now: now))
        XCTAssertFalse(cached.isFresh(for: "hub-2", now: now))
        XCTAssertFalse(cached.isFresh(for: "hub-1", now: now.addingTimeInterval(CachedHubInfo.maxAge)))
    }

    func testHubFailureStatus() {
        XCTAssertEqual(HubFailure.hub(status: 409, detail: "").status, 409)
        XCTAssertEqual(HubFailure.gateway(status: 530, snippet: "").status, 530)
        XCTAssertNil(HubFailure.unreachable("x").status)
        XCTAssertEqual(HubFailure.from(TestError()), .unreachable("the on-device model isn't downloaded"))
        XCTAssertEqual(HubFailure.from(HubFailure.badResponse("x")), .badResponse("x"))
    }
}

/// Bonjour results, the computer link, and the URL rules the pairing relies on.
final class ComputerTests: XCTestCase {
    func testDiscoveredComputerFromTXT() {
        let pc = DiscoveredComputer(serviceName: "DESKTOP-SAMPLE", txt: ["v": "1", "id": "hub-1", "name": "Sample-Desktop", "ver": "2.0.0"])
        XCTAssertEqual(pc.id, "hub-1")
        XCTAssertEqual(pc.name, "Sample-Desktop")
        XCTAssertEqual(pc.version, "2.0.0")
        XCTAssertTrue(pc.isCompatible)

        let bare = DiscoveredComputer(serviceName: "DESKTOP-SAMPLE", txt: [:])
        XCTAssertEqual(bare.id, "DESKTOP-SAMPLE")
        XCTAssertEqual(bare.name, "DESKTOP-SAMPLE")
        XCTAssertTrue(bare.isCompatible)

        XCTAssertFalse(DiscoveredComputer(serviceName: "X", txt: ["v": "2"]).isCompatible)
    }

    func testOneRowPerComputerSortedByName() {
        let list = DiscoveredComputer.unique([
            DiscoveredComputer(serviceName: "Zed", txt: ["id": "z", "name": "Zed"]),
            DiscoveredComputer(serviceName: "alpha", txt: ["id": "a", "name": "alpha"]),
            DiscoveredComputer(serviceName: "Zed", txt: ["id": "z", "name": "Zed"]),
        ])
        XCTAssertEqual(list.map(\.id), ["a", "z"])
    }

    private let legacy = HubConfiguration(
        lanURL: URL(string: "http://192.168.1.20:8766"),
        publicURL: URL(string: "https://dictate.example.com"),
        homeSSID: nil
    )
    private let legacySecrets = HubSecrets(token: "legacy-token", cfClientID: "id.access", cfClientSecret: "secret")
    private let paired = PairedComputer(id: "hub-1", name: "Sample-Desktop", lanURL: URL(string: "http://192.168.1.30:8766")!)

    func testLinkFromDevicePairingOnly() throws {
        let link = try XCTUnwrap(ComputerLink.make(paired: paired, deviceToken: "dev", legacy: HubConfiguration(), legacySecrets: nil, useTunnel: true))
        XCTAssertEqual(link.lanEndpoint?.baseURL, paired.lanURL)
        XCTAssertEqual(link.lanEndpoint?.token, "dev")
        XCTAssertEqual(link.hubID, "hub-1")
        XCTAssertEqual(link.hubKey, "hub-1")
        XCTAssertNil(link.tunnel)
    }

    func testLinkFromLegacyPairingOnly() throws {
        let link = try XCTUnwrap(ComputerLink.make(paired: nil, deviceToken: nil, legacy: legacy, legacySecrets: legacySecrets, useTunnel: true))
        XCTAssertEqual(link.lanEndpoint?.token, "legacy-token")
        XCTAssertNil(link.hubID)
        XCTAssertEqual(link.hubKey, "http://192.168.1.20:8766")
        XCTAssertEqual(link.tunnel?.cfClientID, "id.access")
        XCTAssertEqual(link.tunnel?.route, .tunnel)

        let noTunnel = try XCTUnwrap(ComputerLink.make(paired: nil, deviceToken: nil, legacy: legacy, legacySecrets: legacySecrets, useTunnel: false))
        XCTAssertNil(noTunnel.tunnel)
    }

    func testDevicePairingWinsOnTheLANAndKeepsTheOwnersTunnel() throws {
        let link = try XCTUnwrap(ComputerLink.make(paired: paired, deviceToken: "dev", legacy: legacy, legacySecrets: legacySecrets, useTunnel: true))
        XCTAssertEqual(link.lanEndpoint?.baseURL, paired.lanURL)
        XCTAssertEqual(link.lanEndpoint?.token, "dev")
        XCTAssertEqual(link.tunnel?.token, "legacy-token")
    }

    func testNoCredentialsNoLink() {
        XCTAssertNil(ComputerLink.make(paired: nil, deviceToken: nil, legacy: legacy, legacySecrets: nil, useTunnel: true))
        XCTAssertNil(ComputerLink.make(paired: paired, deviceToken: nil, legacy: HubConfiguration(), legacySecrets: nil, useTunnel: true))
        XCTAssertNil(ComputerLink.make(paired: nil, deviceToken: nil, legacy: HubConfiguration(), legacySecrets: legacySecrets, useTunnel: true))
    }

    func testURLRules() {
        XCTAssertTrue(URLRules.isVPNHost("100.64.0.1"))
        XCTAssertTrue(URLRules.isVPNHost("100.127.255.254"))
        XCTAssertFalse(URLRules.isVPNHost("100.128.0.1"))
        XCTAssertFalse(URLRules.isVPNHost("192.168.1.20"))
        XCTAssertEqual(URLRules.lanBaseURL("192.168.1.20:8766")?.absoluteString, "http://192.168.1.20:8766")
        XCTAssertEqual(URLRules.lanBaseURL("http://192.168.1.20:8766/hello")?.absoluteString, "http://192.168.1.20:8766")
        XCTAssertNil(URLRules.lanBaseURL("8.8.8.8:8766"))
        XCTAssertNil(URLRules.lanBaseURL("not a url"))
    }
}

/// The Live Activity's content (the mouth in the Dynamic Island).
final class DictationActivityStateTests: XCTestCase {
    private func state(_ phase: DictationActivityState.Phase, target: DictationTarget = .pc, name: String? = "Sample-Desktop") -> DictationActivityState {
        DictationActivityState(phase: phase, target: target, computerName: name, startedAt: Date(timeIntervalSince1970: 1_000))
    }

    func testLevelSteps() {
        XCTAssertEqual(DictationActivityState.levelStep(for: 0), 0)
        XCTAssertEqual(DictationActivityState.levelStep(for: 0.5), 2)
        XCTAssertEqual(DictationActivityState.levelStep(for: 1), 4)
        XCTAssertEqual(DictationActivityState.levelStep(for: 3), 4)
        XCTAssertEqual(DictationActivityState.levelStep(for: -1), 0)
        XCTAssertEqual(DictationActivityState.levelStep(for: .nan), 0)
        var listening = state(.listening)
        listening.level = 2
        XCTAssertEqual(listening.openness, 0.5)
    }

    func testWords() {
        XCTAssertEqual(state(.listening).summary, "Listening · to Sample-Desktop")
        XCTAssertEqual(state(.listening, name: nil).summary, "Listening · to Computer")
        XCTAssertEqual(state(.listening, target: .phone).detail, "to Phone")
        XCTAssertEqual(state(.sending).headline, "Sending")
        XCTAssertEqual(state(.sending, target: .phone).detail, "for this iPhone…")
        var done = state(.done)
        done.outcome = .pasted
        done.message = "on Sample-Desktop"
        XCTAssertEqual(done.headline, "Pasted!")
        XCTAssertEqual(done.badge, "Pasted")
        XCTAssertEqual(done.detail, "on Sample-Desktop")
        done.outcome = .copied
        XCTAssertEqual(done.badge, "Copied")
        XCTAssertEqual(done.headline, "Copied!")
        // Finished in the background: not on the clipboard until a tap opens the app.
        done.tapToCopy = true
        XCTAssertEqual(done.badge, "Copy ›")
        XCTAssertEqual(done.headline, "Ready!")
        done.outcome = .pasted
        XCTAssertEqual(done.headline, "Pasted!")
    }

    func testTapToCopyDetail() {
        XCTAssertEqual(DictationActivityState.doneDetail(.copied, notice: nil, computerName: nil, tapToCopy: true), "Tap here to copy it")
        XCTAssertEqual(
            DictationActivityState.doneDetail(.copied, notice: DictationNotice.away, computerName: nil, tapToCopy: true),
            "PC away · tap here to copy it"
        )
        XCTAssertEqual(DictationActivityState.doneDetail(.pasted, notice: nil, computerName: nil, tapToCopy: true), "on your PC")
    }

    func testDoneOutcomes() {
        XCTAssertEqual(DictationActivityState.outcome(pasted: true, text: "x"), .pasted)
        XCTAssertEqual(DictationActivityState.outcome(pasted: false, text: "x"), .copied)
        XCTAssertEqual(DictationActivityState.outcome(pasted: true, text: ""), .nothing)
        XCTAssertEqual(DictationActivityState.doneDetail(.pasted, notice: nil, computerName: "Sample-Desktop"), "on Sample-Desktop")
        XCTAssertEqual(DictationActivityState.doneDetail(.pasted, notice: nil, computerName: nil), "on your PC")
        XCTAssertEqual(DictationActivityState.doneDetail(.copied, notice: nil, computerName: nil), "on your clipboard")
        XCTAssertEqual(
            DictationActivityState.doneDetail(.copied, notice: DictationNotice.away, computerName: "Sample-Desktop"),
            "PC away, so it's on your clipboard"
        )
    }

    func testShortReason() {
        XCTAssertEqual(DictationActivityState.shortReason("Microphone is off. Turn it on."), "Microphone is off")
        let long = DictationActivityState.shortReason("Your PC isn't reachable, and this iPhone couldn't write it down: no model")
        XCTAssertLessThanOrEqual(long.count, 40)
        XCTAssertTrue(long.hasSuffix("…"))
    }

    func testStateIsSmallAndRoundTrips() throws {
        var full = state(.failed)
        full.message = String(repeating: "x", count: 200)
        let encoded = try JSONEncoder().encode(full)
        XCTAssertLessThan(encoded.count, 4096)
        XCTAssertEqual(try JSONDecoder().decode(DictationActivityState.self, from: encoded), full)
    }

    func testLevelThrottle() {
        var throttle = ActivityLevelThrottle(minInterval: 0.25)
        let start = Date(timeIntervalSince1970: 0)
        XCTAssertTrue(throttle.shouldSend(step: 1, at: start))
        XCTAssertFalse(throttle.shouldSend(step: 1, at: start.addingTimeInterval(1)))
        XCTAssertFalse(throttle.shouldSend(step: 3, at: start.addingTimeInterval(0.1)))
        XCTAssertTrue(throttle.shouldSend(step: 3, at: start.addingTimeInterval(0.3)))
        throttle.reset()
        XCTAssertTrue(throttle.shouldSend(step: 3, at: start.addingTimeInterval(0.31)))
    }
}
