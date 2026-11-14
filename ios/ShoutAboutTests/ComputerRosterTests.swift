import XCTest

/// Several paired computers: their short names, which one is selected, and who has the code
/// pairing (its tunnel must never reach a PC other than the selected one).
final class ComputerNamesTests: XCTestCase {
    func testWindowsMadeUpNamesShowAsDesktopOrLaptop() {
        XCTAssertEqual(ComputerNames.pretty("DESKTOP-Q0DB401"), "Desktop")
        XCTAssertEqual(ComputerNames.pretty("LAPTOP-AB12CD3"), "Laptop")
        XCTAssertEqual(ComputerNames.pretty("desktop-q0db401"), "Desktop")
        XCTAssertEqual(ComputerNames.pretty("  DESKTOP-Q0DB401 "), "Desktop")
    }

    func testOtherNamesStayAsTheyAre() {
        XCTAssertEqual(ComputerNames.pretty("Sam's PC"), "Sam's PC")
        XCTAssertEqual(ComputerNames.pretty("DESKTOP-Q0DB40"), "DESKTOP-Q0DB40")  // 6 characters
        XCTAssertEqual(ComputerNames.pretty("DESKTOP-Q0DB4012"), "DESKTOP-Q0DB4012")  // 8 characters
        XCTAssertEqual(ComputerNames.pretty("DESKTOP-Q0DB-01"), "DESKTOP-Q0DB-01")
        XCTAssertEqual(ComputerNames.pretty("MY-DESKTOP-Q0DB401"), "MY-DESKTOP-Q0DB401")
        XCTAssertEqual(ComputerNames.pretty("Desktop"), "Desktop")
        XCTAssertEqual(ComputerNames.pretty("SERVER-Q0DB401"), "SERVER-Q0DB401")
    }

    func testNicknameWins() {
        XCTAssertEqual(ComputerNames.display(nickname: "Office", name: "DESKTOP-Q0DB401"), "Office")
        XCTAssertEqual(ComputerNames.display(nickname: "  ", name: "DESKTOP-Q0DB401"), "Desktop")
        XCTAssertEqual(ComputerNames.display(nickname: nil, name: "Sam's PC"), "Sam's PC")
        XCTAssertEqual(ComputerNames.display(nickname: nil, name: " "), "Computer")
    }

    func testRepeatedNamesGetANumber() {
        XCTAssertEqual(ComputerNames.distinct(["Desktop", "Laptop"]), ["Desktop", "Laptop"])
        XCTAssertEqual(ComputerNames.distinct(["Desktop", "Desktop"]), ["Desktop", "Desktop 2"])
        XCTAssertEqual(ComputerNames.distinct(["Desktop", "desktop", "Desktop"]), ["Desktop", "desktop 2", "Desktop 3"])
        XCTAssertEqual(ComputerNames.distinct(["Desktop", "Desktop 2", "Desktop"]), ["Desktop", "Desktop 2", "Desktop 3"])
        XCTAssertEqual(ComputerNames.distinct([]), [])
    }
}

final class ComputerRosterTests: XCTestCase {
    private let desktop = PairedComputer(
        id: "hub-desktop", name: "DESKTOP-Q0DB401",
        lanURL: URL(string: "http://192.168.1.20:8766")!, pairedAt: Date(timeIntervalSince1970: 1_700_000_000)
    )
    private let laptop = PairedComputer(
        id: "hub-laptop", name: "LAPTOP-AB12CD3",
        lanURL: URL(string: "http://192.168.1.21:8766")!, pairedAt: Date(timeIntervalSince1970: 1_700_000_100)
    )
    private let codePairing = HubConfiguration(
        lanURL: URL(string: "http://192.168.1.20:8765"),
        publicURL: URL(string: "https://dictate.example.com"),
        homeSSID: nil
    )

    func testAddingSelectsTheNewComputer() {
        var roster = ComputerRoster()
        XCTAssertNil(roster.selected)
        roster.add(desktop, codePairing: nil)
        XCTAssertEqual(roster.selected?.id, desktop.id)
        roster.add(laptop, codePairing: nil)
        XCTAssertEqual(roster.computers.map(\.id), [desktop.id, laptop.id])
        XCTAssertEqual(roster.selected?.id, laptop.id)
        XCTAssertEqual(roster.displayNames, [desktop.id: "Desktop", laptop.id: "Laptop"])
    }

    func testPairingTheSamePCAgainReplacesItAndKeepsItsNickname() {
        var roster = ComputerRoster()
        roster.add(desktop, codePairing: nil)
        roster.add(laptop, codePairing: nil)
        roster.rename(desktop.id, to: "Office")
        var again = desktop
        again.name = "Desktop"
        again.lanURL = URL(string: "http://192.168.1.40:8766")!
        roster.add(again, codePairing: nil)
        XCTAssertEqual(roster.computers.count, 2)
        XCTAssertEqual(roster.computers.first?.lanURL, again.lanURL)
        XCTAssertEqual(roster.computers.first?.name, "Desktop")
        XCTAssertEqual(roster.computers.first?.nickname, "Office")
        XCTAssertEqual(roster.selected?.id, desktop.id)
    }

    func testRemovingTheSelectedOneSelectsAnother() {
        var roster = ComputerRoster(computers: [desktop, laptop], selectedID: laptop.id)
        XCTAssertFalse(roster.remove(laptop.id))
        XCTAssertEqual(roster.selected?.id, desktop.id)
        roster.remove(desktop.id)
        XCTAssertNil(roster.selected)
        XCTAssertTrue(roster.computers.isEmpty)
    }

    func testRemovingAnotherKeepsTheSelection() {
        var roster = ComputerRoster(computers: [desktop, laptop], selectedID: desktop.id)
        roster.remove(laptop.id)
        XCTAssertEqual(roster.selected?.id, desktop.id)
    }

    func testSelectOnlyKnownComputers() {
        var roster = ComputerRoster(computers: [desktop, laptop], selectedID: desktop.id)
        XCTAssertTrue(roster.select(laptop.id))
        XCTAssertFalse(roster.select(laptop.id))  // already
        XCTAssertFalse(roster.select("someone-else"))
        XCTAssertEqual(roster.selected?.id, laptop.id)
    }

    func testNormalizeFixesAMissingSelectionAndOwner() {
        var roster = ComputerRoster(computers: [desktop, laptop], selectedID: "gone", codePairingOwnerID: "gone")
        roster.normalize()
        XCTAssertEqual(roster.selected?.id, desktop.id)
        XCTAssertNil(roster.codePairingOwnerID)
        var empty = ComputerRoster(selectedID: "gone")
        empty.normalize()
        XCTAssertNil(empty.selectedID)
    }

    func testRenameAndClear() {
        var roster = ComputerRoster(computers: [desktop], selectedID: desktop.id)
        roster.rename(desktop.id, to: "  Office  ")
        XCTAssertEqual(roster.displayName(for: desktop.id), "Office")
        roster.rename(desktop.id, to: String(repeating: "x", count: 100))
        XCTAssertEqual(roster.computers[0].nickname?.count, ComputerRoster.maxNicknameLength)
        roster.rename(desktop.id, to: "")
        XCTAssertNil(roster.computers[0].nickname)
        XCTAssertEqual(roster.displayName(for: desktop.id), "Desktop")
    }

    func testTheSameShortNameTwiceGetsANumber() {
        var second = laptop
        second.name = "DESKTOP-ZZZZ999"
        let roster = ComputerRoster(computers: [desktop, second], selectedID: desktop.id)
        XCTAssertEqual(roster.displayName(for: desktop.id), "Desktop")
        XCTAssertEqual(roster.displayName(for: second.id), "Desktop 2")
        let choices = roster.choices(usable: [desktop.id])
        XCTAssertEqual(choices.map(\.name), ["Desktop", "Desktop 2"])
        XCTAssertEqual(choices.map(\.usable), [true, false])
        XCTAssertEqual(choices.map(\.reportedName), ["DESKTOP-Q0DB401", "DESKTOP-ZZZZ999"])
        XCTAssertFalse(choices.contains(where: \.isCodePairing))
    }

    func testReportedNamesAndAddressesAreKeptFresh() {
        var roster = ComputerRoster(computers: [desktop], selectedID: desktop.id)
        XCTAssertTrue(roster.noteName("Desktop", for: desktop.id))
        XCTAssertFalse(roster.noteName("Desktop", for: desktop.id))
        XCTAssertFalse(roster.noteName("  ", for: desktop.id))
        XCTAssertFalse(roster.noteName(nil, for: desktop.id))
        XCTAssertFalse(roster.noteName("Other", for: "unknown"))
        XCTAssertEqual(roster.computers[0].name, "Desktop")

        roster.rename(desktop.id, to: "Office")
        roster.noteName("Gaming PC", for: desktop.id)
        XCTAssertEqual(roster.displayName(for: desktop.id), "Office")  // the nickname still wins

        let moved = URL(string: "http://192.168.1.77:8766")!
        XCTAssertTrue(roster.noteAddress(moved, for: desktop.id))
        XCTAssertFalse(roster.noteAddress(moved, for: desktop.id))
        XCTAssertEqual(roster.selected?.lanURL, moved)
    }

    // MARK: The code pairing (QR: hub token + tunnel)

    func testOnlyTheComputerAtTheCodesAddressTakesTheCodePairing() {
        var roster = ComputerRoster()
        roster.add(laptop, codePairing: codePairing)  // 192.168.1.21: not the code's PC
        XCTAssertNil(roster.codePairingOwnerID)
        roster.add(desktop, codePairing: codePairing)  // 192.168.1.20, like the code's lan_url
        XCTAssertEqual(roster.codePairingOwnerID, desktop.id)
        // Pairing the laptop again doesn't take it over.
        roster.add(laptop, codePairing: codePairing)
        XCTAssertEqual(roster.codePairingOwnerID, desktop.id)
    }

    func testTheFirstComputerTakesItsOwnCodePairing() {
        var roster = ComputerRoster()
        roster.add(desktop, codePairing: codePairing)
        XCTAssertEqual(roster.codePairingOwnerID, desktop.id)
    }

    func testNoCodePairingNoOwner() {
        var roster = ComputerRoster()
        roster.add(desktop, codePairing: nil)
        XCTAssertNil(roster.codePairingOwnerID)
        roster.add(desktop, codePairing: HubConfiguration())
        XCTAssertNil(roster.codePairingOwnerID)
    }

    func testRemovingTheOwnerTakesTheCodePairingAlong() {
        var roster = ComputerRoster(computers: [desktop, laptop], selectedID: desktop.id, codePairingOwnerID: desktop.id)
        XCTAssertTrue(roster.remove(desktop.id))
        XCTAssertNil(roster.codePairingOwnerID)
    }

    func testAScannedCodeGoesToTheComputerAtItsAddress() {
        var roster = ComputerRoster(computers: [desktop, laptop], selectedID: laptop.id)
        roster.assignCodePairing(lanURL: URL(string: "http://192.168.1.20:8765"))
        XCTAssertEqual(roster.codePairingOwnerID, desktop.id)
    }

    func testAScannedCodeWithNoMatchBelongsToNoComputer() {
        // Never guessed, not even with one computer: its tunnel could reach another PC.
        var one = ComputerRoster(computers: [desktop], selectedID: desktop.id, codePairingOwnerID: desktop.id)
        one.assignCodePairing(lanURL: URL(string: "http://10.0.0.9:8765"))
        XCTAssertNil(one.codePairingOwnerID)

        var two = ComputerRoster(computers: [desktop, laptop], selectedID: laptop.id)
        two.assignCodePairing(lanURL: nil)  // tunnel only
        XCTAssertNil(two.codePairingOwnerID)
    }

    // MARK: From the version with one computer

    func testAdoptingTheSingleComputer() {
        var roster = ComputerRoster()
        roster.adopt(desktop, hasCodePairing: true)
        XCTAssertEqual(roster.computers, [desktop])
        XCTAssertEqual(roster.selected?.id, desktop.id)
        XCTAssertEqual(roster.codePairingOwnerID, desktop.id)
        // Again (the token couldn't move the first time): nothing changes.
        let before = roster
        roster.adopt(desktop, hasCodePairing: true)
        XCTAssertEqual(roster, before)
    }

    func testAdoptingWithoutCodePairing() {
        var roster = ComputerRoster()
        roster.adopt(desktop, hasCodePairing: false)
        XCTAssertNil(roster.codePairingOwnerID)
    }

    func testOldSavedComputersDecodeWithoutANickname() throws {
        let json = #"{"id":"hub-1","name":"DESKTOP-Q0DB401","lanURL":"http:\/\/192.168.1.20:8766","pairedAt":"2023-11-14T22:13:20Z"}"#
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let computer = try decoder.decode(PairedComputer.self, from: Data(json.utf8))
        XCTAssertNil(computer.nickname)
        XCTAssertEqual(computer.displayName, "Desktop")
    }
}
