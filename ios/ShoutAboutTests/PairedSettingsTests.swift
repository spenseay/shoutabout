import XCTest

/// What v2 pairing keeps in the App Group (never the token: that's in the keychain).
final class PairedSettingsTests: XCTestCase {
    private func makeSettings() -> SharedSettings {
        let suite = "shoutabout-tests-\(UUID().uuidString)"
        addTeardownBlock {
            UserDefaults.standard.removePersistentDomain(forName: suite)
        }
        return SharedSettings(defaults: UserDefaults(suiteName: suite) ?? .standard)
    }

    func testDefaults() {
        let settings = makeSettings()
        XCTAssertNil(settings.pairedComputer)
        XCTAssertNil(settings.cachedHubInfo)
        XCTAssertFalse(settings.onboardingDone)
        XCTAssertTrue(settings.useTunnel)
    }

    func testPairedComputerRoundTrip() {
        let settings = makeSettings()
        let computer = PairedComputer(
            id: "hub-1",
            name: "Sample-Desktop",
            lanURL: URL(string: "http://192.168.1.20:8766")!,
            pairedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )
        settings.pairedComputer = computer
        XCTAssertEqual(SharedSettings(defaults: settings.defaults).pairedComputer, computer)
        settings.pairedComputer = nil
        XCTAssertNil(settings.pairedComputer)
    }

    func testCachedInfoRoundTrip() {
        let settings = makeSettings()
        let cached = CachedHubInfo(info: .sample(speed: "slow"), fetchedAt: Date(timeIntervalSince1970: 1_700_000_000), hubKey: "hub-1")
        settings.cachedHubInfo = cached
        XCTAssertEqual(settings.cachedHubInfo, cached)
    }

    func testDeviceIDIsCreatedOnceAndKept() {
        let settings = makeSettings()
        let first = settings.deviceID()
        XCTAssertFalse(first.isEmpty)
        XCTAssertNotNil(UUID(uuidString: first))
        XCTAssertEqual(settings.deviceID(), first)
        XCTAssertEqual(SharedSettings(defaults: settings.defaults).deviceID(), first)
    }

    func testTargetLabels() {
        XCTAssertEqual(DictationTarget.pc.label, "Computer")
        XCTAssertEqual(DictationTarget.phone.label, "Phone")
        XCTAssertEqual(DictationTarget.pc.rawValue, "pc")
    }
}
