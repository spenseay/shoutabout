import Foundation

/// Recent dictations, kept on this iPhone only (Application Support/history.json, excluded
/// from iCloud backup). Text never leaves the device through this store.
struct HistoryStore: Sendable {
    static let limit = 200

    var fileURL: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("ShoutAbout", isDirectory: true).appendingPathComponent("history.json")
    }()

    func load() -> [HistoryItem] {
        guard let data = try? Data(contentsOf: fileURL) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return (try? decoder.decode([HistoryItem].self, from: data)) ?? []
    }

    func save(_ items: [HistoryItem]) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(Array(items.prefix(Self.limit))) else { return }
        let folder = fileURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        var url = fileURL
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
    }
}
