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

    private let desktop = PairedComputer(
        id: "hub-1",
        name: "DESKTOP-Q0DB401",
        lanURL: URL(string: "http://192.168.1.20:8766")!,
        pairedAt: Date(timeIntervalSince1970: 1_700_000_000)
    )
    private let laptop = PairedComputer(
        id: "hub-2",
        name: "Sample-Laptop",
        lanURL: URL(string: "http://192.168.1.21:8766")!,
        pairedAt: Date(timeIntervalSince1970: 1_700_000_100),
        nickname: "Couch"
    )

    func testDefaults() {
        let settings = makeSettings()
        XCTAssertEqual(settings.roster, ComputerRoster())
        XCTAssertNil(settings.selectedComputerName)
        XCTAssertTrue(settings.cachedHubInfos.isEmpty)
        XCTAssertNil(settings.pairedComputer)
        XCTAssertNil(settings.cachedHubInfo)
        XCTAssertFalse(settings.onboardingDone)
        XCTAssertTrue(settings.useTunnel)
    }

    func testRosterRoundTrip() {
        let settings = makeSettings()
        let roster = ComputerRoster(computers: [desktop, laptop], selectedID: laptop.id, codePairingOwnerID: desktop.id)
        settings.roster = roster
        let again = SharedSettings(defaults: settings.defaults)
        XCTAssertEqual(again.roster, roster)
        XCTAssertEqual(again.selectedComputerName, "Couch")
        settings.roster = ComputerRoster()
        XCTAssertEqual(settings.roster, ComputerRoster())
    }

    func testAMissingSelectionFallsBackToTheFirstComputer() {
        let settings = makeSettings()
        settings.roster = ComputerRoster(computers: [desktop, laptop], selectedID: nil)
        XCTAssertEqual(settings.roster.selected?.id, desktop.id)
        XCTAssertEqual(settings.selectedComputerName, "Desktop")
    }

    func testCachedInfoRoundTrip() {
        let settings = makeSettings()
        let first = CachedHubInfo(info: .sample(speed: "slow"), fetchedAt: Date(timeIntervalSince1970: 1_700_000_000), hubKey: "hub-1")
        let second = CachedHubInfo(info: .sample(speed: "fast"), fetchedAt: Date(timeIntervalSince1970: 1_700_000_000), hubKey: "hub-2")
        settings.cachedHubInfos = [first, second]
        XCTAssertEqual(SharedSettings(defaults: settings.defaults).cachedHubInfos, [first, second])
        settings.cachedHubInfos = []
        XCTAssertTrue(settings.cachedHubInfos.isEmpty)
    }

    // MARK: From the version with one computer

    func testTheSingleComputerBecomesTheFirstOfTheList() {
        let settings = makeSettings()
        let cached = CachedHubInfo(info: .sample(speed: "fast"), fetchedAt: Date(timeIntervalSince1970: 1_700_000_000), hubKey: "hub-1")
        settings.pairedComputer = desktop
        settings.cachedHubInfo = cached
        var moved: [String] = []
        settings.migrateSingleComputer(hasCodePairing: true) { id in
            moved.append(id)
            return true
        }
        XCTAssertEqual(moved, ["hub-1"])
        XCTAssertEqual(settings.roster.computers, [desktop])
        XCTAssertEqual(settings.roster.selected?.id, desktop.id)
        XCTAssertEqual(settings.roster.codePairingOwnerID, desktop.id)
        XCTAssertEqual(settings.cachedHubInfos, [cached])
        XCTAssertNil(settings.pairedComputer)
        XCTAssertNil(settings.cachedHubInfo)

        // Done once: the next launch changes nothing.
        settings.migrateSingleComputer(hasCodePairing: true) { id in
            moved.append(id)
            return true
        }
        XCTAssertEqual(moved, ["hub-1"])
        XCTAssertEqual(settings.roster.computers, [desktop])
    }

    func testALockedKeychainKeepsTheOldEntryForNextTime() {
        let settings = makeSettings()
        settings.pairedComputer = desktop
        settings.migrateSingleComputer(hasCodePairing: false) { _ in false }
        XCTAssertEqual(settings.roster.computers, [desktop])
        XCTAssertNil(settings.roster.codePairingOwnerID)
        XCTAssertEqual(settings.pairedComputer, desktop)  // tried again next launch

        settings.migrateSingleComputer(hasCodePairing: false) { _ in true }
        XCTAssertEqual(settings.roster.computers, [desktop])  // not twice
        XCTAssertNil(settings.pairedComputer)
    }

    func testNothingToMigrateWritesNothing() {
        let settings = makeSettings()
        var called = false
        settings.migrateSingleComputer(hasCodePairing: true) { _ in
            called = true
            return true
        }
        XCTAssertFalse(called)
        XCTAssertNil(settings.defaults.object(forKey: SharedSettings.Key.pairedComputers))
        XCTAssertNil(settings.defaults.object(forKey: SharedSettings.Key.selectedComputerID))
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
