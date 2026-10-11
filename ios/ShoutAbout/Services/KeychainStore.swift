import Foundation
import Security

/// Hub secrets (bearer token, Cloudflare Access service token) in the keychain.
///
/// Items go into the App Group's keychain access group (an App Group identifier is a valid
/// keychain access group), so a future keyboard or widget extension in the same group can read
/// them without a separate Keychain Sharing capability. Accessible after first unlock, so the
/// Action Button works while the phone is locked. Never synchronised to iCloud.
struct KeychainStore: Sendable {
    enum Item: String, CaseIterable, Sendable {
        /// Legacy QR pairing: the hub's shared token.
        case token = "hub_token"
        case cfClientID = "cf_client_id"
        case cfClientSecret = "cf_client_secret"
        /// v2 pairing: this iPhone's own token, minted by the PC when you click Allow.
        case deviceToken = "device_token"

        static let legacyItems: [Item] = [.token, .cfClientID, .cfClientSecret]
    }

    enum KeychainError: Error, LocalizedError {
        case status(OSStatus)

        var errorDescription: String? {
            switch self {
            case .status(let status):
                let message = SecCopyErrorMessageString(status, nil) as String? ?? "unknown"
                return "Keychain error \(status): \(message)"
            }
        }
    }

    var service = "fyi.shoutabout.hub"
    var accessGroup: String? = AppGroup.id

    func read(_ item: Item) -> String? {
        if let accessGroup, let value = read(item, group: accessGroup) { return value }
        return read(item, group: nil)
    }

    func secrets() -> HubSecrets? {
        guard let token = read(.token), !token.isEmpty else { return nil }
        return HubSecrets(token: token, cfClientID: read(.cfClientID), cfClientSecret: read(.cfClientSecret))
    }

    func save(_ secrets: HubSecrets) throws {
        try write(secrets.token, for: .token)
        try write(secrets.cfClientID, for: .cfClientID)
        try write(secrets.cfClientSecret, for: .cfClientSecret)
    }

    func deviceToken() -> String? {
        guard let token = read(.deviceToken), !token.isEmpty else { return nil }
        return token
    }

    func saveDeviceToken(_ token: String?) throws {
        try write(token, for: .deviceToken)
    }

    /// Removes the legacy hub token and the Cloudflare service token.
    func deleteLegacy() {
        for item in Item.legacyItems {
            delete(item, group: accessGroup)
            delete(item, group: nil)
        }
    }

    func deleteAll() {
        for item in Item.allCases {
            delete(item, group: accessGroup)
            delete(item, group: nil)
        }
    }

    /// Writes (or deletes, for nil) one item. Falls back to the app's default access group when
    /// the build has no entitlement for the shared one (unsigned simulator builds).
    func write(_ value: String?, for item: Item) throws {
        delete(item, group: accessGroup)
        delete(item, group: nil)
        guard let value, !value.isEmpty else { return }
        var status = add(value, item: item, group: accessGroup)
        if status == errSecMissingEntitlement, accessGroup != nil {
            status = add(value, item: item, group: nil)
        }
        guard status == errSecSuccess else { throw KeychainError.status(status) }
    }

    // MARK: Low level

    private func baseQuery(_ item: Item, group: String?) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: item.rawValue,
            kSecAttrSynchronizable as String: false,
        ]
        if let group { query[kSecAttrAccessGroup as String] = group }
        return query
    }

    private func read(_ item: Item, group: String?) -> String? {
        var query = baseQuery(item, group: group)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func add(_ value: String, item: Item, group: String?) -> OSStatus {
        var query = baseQuery(item, group: group)
        query[kSecValueData as String] = Data(value.utf8)
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(query as CFDictionary, nil)
    }

    @discardableResult
    private func delete(_ item: Item, group: String?) -> OSStatus {
        SecItemDelete(baseQuery(item, group: group) as CFDictionary)
    }
}
