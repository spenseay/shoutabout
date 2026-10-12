import Foundation

/// Recent: the last few dictations, in memory only. Never written to disk, gone when the app
/// quits (or iOS ends it). It's only there to get back text that landed in the wrong spot.
struct HistoryStore: Sendable {
    static let limit = 3

    /// Newest first, at most `limit`.
    private(set) var items: [HistoryItem]

    init(_ items: [HistoryItem] = []) {
        self.items = Array(items.filter { !$0.text.isEmpty }.prefix(Self.limit))
    }

    /// Puts a finished dictation at the top and lets the oldest go past `limit`. One without
    /// words (failed, nothing heard) isn't kept: there's nothing to get back.
    mutating func add(_ item: HistoryItem) {
        guard !item.text.isEmpty else { return }
        items.insert(item, at: 0)
        if items.count > Self.limit {
            items.removeLast(items.count - Self.limit)
        }
    }

    mutating func remove(_ ids: Set<UUID>) {
        items.removeAll { ids.contains($0.id) }
    }
}

// MARK: - What older versions saved

extension HistoryStore {
    /// Where versions before Recent kept up to 200 dictations, when Settings › Keep history
    /// was on.
    static var legacyFileURL: URL? {
        guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        return base.appendingPathComponent("ShoutAbout", isDirectory: true).appendingPathComponent("history.json")
    }

    /// The App Group key of that old switch.
    static let legacyKeepHistoryKey = "keepHistory"

    /// Deletes what older versions saved: the history file, its folder when nothing else is in
    /// it, and the old switch. Once they're gone (after the first launch of this version) it
    /// finds nothing and does nothing; if a delete fails, the next launch tries again.
    static func removeSavedHistory(defaults: UserDefaults, fileURL: URL? = HistoryStore.legacyFileURL) {
        if defaults.object(forKey: legacyKeepHistoryKey) != nil {
            defaults.removeObject(forKey: legacyKeepHistoryKey)
        }
        guard let fileURL else { return }
        let manager = FileManager.default
        if manager.fileExists(atPath: fileURL.path(percentEncoded: false)) {
            try? manager.removeItem(at: fileURL)
        }
        let folder = fileURL.deletingLastPathComponent()
        if let contents = try? manager.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil),
           contents.isEmpty {
            try? manager.removeItem(at: folder)
        }
    }
}
