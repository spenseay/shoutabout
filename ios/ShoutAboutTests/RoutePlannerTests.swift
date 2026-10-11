import XCTest

final class RoutePlannerTests: XCTestCase {
    private func inputs(
        lan: Bool = true,
        pub: Bool = true,
        wifi: Bool = true,
        preferLAN: Bool = true,
        offline: Bool = false,
        fallback: Bool = true
    ) -> RoutePlanner.Inputs {
        RoutePlanner.Inputs(
            hasLANURL: lan,
            hasPublicURL: pub,
            onWiFi: wifi,
            preferLAN: preferLAN,
            forceOffline: offline,
            offlineFallback: fallback
        )
    }

    func testOnWiFiTriesLANThenTunnelThenOffline() {
        XCTAssertEqual(RoutePlanner.candidates(inputs()), [.lan, .tunnel, .offline])
    }

    func testOnWiFiWithoutPreferLAN() {
        XCTAssertEqual(RoutePlanner.candidates(inputs(preferLAN: false)), [.tunnel, .lan, .offline])
    }

    func testOnCellularSkipsLAN() {
        XCTAssertEqual(RoutePlanner.candidates(inputs(wifi: false)), [.tunnel, .offline])
    }

    func testOnCellularWithoutPublicURLStillTriesLAN() {
        // e.g. a Tailscale address in the LAN URL
        XCTAssertEqual(RoutePlanner.candidates(inputs(pub: false, wifi: false)), [.lan, .offline])
    }

    func testForcedOffline() {
        XCTAssertEqual(RoutePlanner.candidates(inputs(offline: true)), [.offline])
    }

    func testNoFallback() {
        XCTAssertEqual(RoutePlanner.candidates(inputs(fallback: false)), [.lan, .tunnel])
    }

    func testUnpairedGoesOffline() {
        XCTAssertEqual(RoutePlanner.candidates(inputs(lan: false, pub: false)), [.offline])
        XCTAssertEqual(RoutePlanner.candidates(inputs(lan: false, pub: false, fallback: false)), [])
    }

    func testFailureActions() {
        XCTAssertEqual(RoutePlanner.action(after: .unreachable("timed out")), .tryNext)
        XCTAssertEqual(RoutePlanner.action(after: .gateway(status: 401, snippet: "<html>")), .tryNext)
        XCTAssertEqual(RoutePlanner.action(after: .gateway(status: 530, snippet: "")), .tryNext)
        XCTAssertEqual(RoutePlanner.action(after: .hub(status: 503, detail: "hub is starting")), .skipToOffline)
        XCTAssertEqual(RoutePlanner.action(after: .hub(status: 429, detail: "rate limited")), .skipToOffline)
        XCTAssertEqual(RoutePlanner.action(after: .hub(status: 401, detail: "missing or invalid bearer token")), .stop)
        XCTAssertEqual(RoutePlanner.action(after: .hub(status: 413, detail: "upload too large")), .stop)
        XCTAssertEqual(RoutePlanner.action(after: .hub(status: 415, detail: "unsupported")), .stop)
        XCTAssertEqual(RoutePlanner.action(after: .badResponse("not JSON")), .stop)
    }
}
