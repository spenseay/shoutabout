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
        /// v2 pairing before several computers: the one device token. Each computer's own
        /// token is now under `device_token.<hub id>` (`deviceToken(for:)`).
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
        read(account: item.rawValue)
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

    /// v2 pairing: this iPhone's token for one computer, minted by that PC when you click Allow.
    func deviceToken(for hubID: String) -> String? {
        guard let token = read(account: Self.deviceAccount(hubID)), !token.isEmpty else { return nil }
        return token
    }

    /// Saves (or deletes, for nil) one computer's token.
    func saveDeviceToken(_ token: String?, for hubID: String) throws {
        try write(token, account: Self.deviceAccount(hubID))
    }

    static func deviceAccount(_ hubID: String) -> String {
        "\(Item.deviceToken.rawValue).\(hubID)"
    }

    /// Before several computers there was one device token. Moves it to that computer's own
    /// item (`hubID`). False when the keychain can't be read yet (before the first unlock):
    /// the caller tries again on the next launch. True when there was none.
    func moveSingleDeviceToken(to hubID: String) -> Bool {
        let old = Item.deviceToken.rawValue
        switch lookup(account: old) {
        case .unavailable:
            return false
        case .missing:
            return true
        case .found(let token):
            if deviceToken(for: hubID) == nil {
                do {
                    try write(token, account: Self.deviceAccount(hubID))
                } catch {
                    return false
                }
            }
            delete(account: old, group: accessGroup)
            delete(account: old, group: nil)
            return true
        }
    }

    /// Removes the legacy hub token and the Cloudflare service token.
    func deleteLegacy() {
        for item in Item.legacyItems {
            delete(account: item.rawValue, group: accessGroup)
            delete(account: item.rawValue, group: nil)
        }
    }

    /// Removes the fixed items (not the per-computer device tokens).
    func deleteAll() {
        for item in Item.allCases {
            delete(account: item.rawValue, group: accessGroup)
            delete(account: item.rawValue, group: nil)
        }
    }

    /// Writes (or deletes, for nil) one item.
    func write(_ value: String?, for item: Item) throws {
        try write(value, account: item.rawValue)
    }

    // MARK: Low level

    private enum Lookup {
        case found(String)
        case missing
        /// The keychain can't be read right now (locked, before the first unlock).
        case unavailable
    }

    /// Writes (or deletes, for nil) one account. Falls back to the app's default access group
    /// when the build has no entitlement for the shared one (unsigned simulator builds).
    private func write(_ value: String?, account: String) throws {
        delete(account: account, group: accessGroup)
        delete(account: account, group: nil)
        guard let value, !value.isEmpty else { return }
        var status = add(value, account: account, group: accessGroup)
        if status == errSecMissingEntitlement, accessGroup != nil {
            status = add(value, account: account, group: nil)
        }
        guard status == errSecSuccess else { throw KeychainError.status(status) }
    }

    private func read(account: String) -> String? {
        if case .found(let value) = lookup(account: account) { return value }
        return nil
    }

    /// The shared access group first, then the app's default one.
    private func lookup(account: String) -> Lookup {
        let groups: [String?] = accessGroup == nil ? [nil] : [accessGroup, nil]
        var unavailable = false
        for group in groups {
            switch lookup(account: account, group: group) {
            case .found(let value):
                return .found(value)
            case .unavailable:
                unavailable = true
            case .missing:
                break
            }
        }
        return unavailable ? .unavailable : .missing
    }

    private func lookup(account: String, group: String?) -> Lookup {
        var query = baseQuery(account: account, group: group)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecSuccess, let data = result as? Data, let value = String(data: data, encoding: .utf8) {
            return .found(value)
        }
        return status == errSecInteractionNotAllowed ? .unavailable : .missing
    }

    private func baseQuery(account: String, group: String?) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrSynchronizable as String: false,
        ]
        if let group { query[kSecAttrAccessGroup as String] = group }
        return query
    }

    private func add(_ value: String, account: String, group: String?) -> OSStatus {
        var query = baseQuery(account: account, group: group)
        query[kSecValueData as String] = Data(value.utf8)
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(query as CFDictionary, nil)
    }

    @discardableResult
    private func delete(account: String, group: String?) -> OSStatus {
        SecItemDelete(baseQuery(account: account, group: group) as CFDictionary)
    }
}
