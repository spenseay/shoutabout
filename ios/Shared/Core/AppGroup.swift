import Foundation

/// The App Group shared by the app and its extensions (widgets, keyboard). Its identifier comes
/// from the `ShoutAboutAppGroup` Info.plist key, which the build fills from `APP_GROUP_ID`
/// (Config/Base.xcconfig), so the bundle prefix is configured in exactly one place.
///
/// A file of its own so the keyboard extension can use it without the app's settings model.
enum AppGroup {
    static let fallbackID = "group.fyi.shoutabout.app"

    static var id: String {
        let value = Bundle.main.object(forInfoDictionaryKey: "ShoutAboutAppGroup") as? String
        guard let value, !value.isEmpty, !value.contains("$(") else { return fallbackID }
        return value
    }

    /// Shared defaults. Without the entitlement (unsigned simulator builds, or the keyboard
    /// without Full Access) iOS still returns a working, private store, so callers never need
    /// to handle nil.
    static var defaults: UserDefaults { UserDefaults(suiteName: id) ?? .standard }
}
