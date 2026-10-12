import XCTest

/// Recent (Shared/Core/HistoryStore.swift): the last 3 dictations, newest first, in memory
/// only; and removing what older versions saved on disk.
final class HistoryStoreTests: XCTestCase {
    private func item(_ text: String) -> HistoryItem {
        HistoryItem(text: text, target: .phone, route: .offline)
    }

    func testKeepsOnlyTheLastThreeNewestFirst() {
        XCTAssertEqual(HistoryStore.limit, 3)
        var store = HistoryStore()
        XCTAssertTrue(store.items.isEmpty)
        for text in ["one", "two", "three", "four", "five"] {
            store.add(item(text))
        }
        XCTAssertEqual(store.items.map(\.text), ["five", "four", "three"])
    }

    func testSkipsDictationsWithoutWords() {
        var store = HistoryStore()
        store.add(item("Sample text: call Sofia."))
        store.add(HistoryItem(text: "", target: .pc, route: .offline, error: "Your PC isn't reachable."))
        store.add(item(""))
        XCTAssertEqual(store.items.map(\.text), ["Sample text: call Sofia."])
    }

    func testAStartingListIsCappedTooAndItemsCanBeRemoved() {
        var store = HistoryStore([item("a"), item("b"), item("c"), item("d")])
        XCTAssertEqual(store.items.map(\.text), ["a", "b", "c"])
        let second = store.items[1].id
        store.remove([second])
        XCTAssertEqual(store.items.map(\.text), ["a", "c"])
    }

    // MARK: What older versions saved

    /// A throwaway defaults suite, removed when the test ends.
    private func makeDefaults() throws -> UserDefaults {
        let suite = "shoutabout-history-tests-\(UUID().uuidString)"
        addTeardownBlock {
            UserDefaults.standard.removePersistentDomain(forName: suite)
        }
        return try XCTUnwrap(UserDefaults(suiteName: suite))
    }

    /// <temp>/<random>/ShoutAbout/history.json, like the old Application Support layout.
    private func makeOldHistoryFile() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("shoutabout-history-tests-\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock {
            _ = try? FileManager.default.removeItem(at: root)
        }
        let folder = root.appendingPathComponent("ShoutAbout", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("history.json")
        try Data(#"[{"text": "Sample text: an old dictation."}]"#.utf8).write(to: file)
        return file
    }

    private func exists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }

    func testRemovesTheOldHistoryFileItsFolderAndTheOldSwitch() throws {
        let defaults = try makeDefaults()
        defaults.set(true, forKey: HistoryStore.legacyKeepHistoryKey)
        let file = try makeOldHistoryFile()
        let folder = file.deletingLastPathComponent()

        HistoryStore.removeSavedHistory(defaults: defaults, fileURL: file)
        XCTAssertFalse(exists(file))
        XCTAssertFalse(exists(folder))  // it held only the history
        XCTAssertNil(defaults.object(forKey: HistoryStore.legacyKeepHistoryKey))

        // The next launches find nothing and change nothing.
        HistoryStore.removeSavedHistory(defaults: defaults, fileURL: file)
        XCTAssertFalse(exists(file))
        XCTAssertNil(defaults.object(forKey: HistoryStore.legacyKeepHistoryKey))
    }

    func testKeepsAFolderThatHoldsSomethingElse() throws {
        let defaults = try makeDefaults()
        let file = try makeOldHistoryFile()
        let other = file.deletingLastPathComponent().appendingPathComponent("other.txt")
        try Data("x".utf8).write(to: other)

        HistoryStore.removeSavedHistory(defaults: defaults, fileURL: file)
        XCTAssertFalse(exists(file))
        XCTAssertTrue(exists(other))
    }
}
