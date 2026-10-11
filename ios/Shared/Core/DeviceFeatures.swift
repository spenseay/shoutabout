import Foundation

/// Which iPhones have an Action Button, from the model identifier ("iPhone16,1", from uname,
/// a public API; there's no API that says it directly). The iPhone 15 Pro and Pro Max
/// (iPhone16,1 and 16,2) and every iPhone since (iPhone17,x and later: 16, 16e, 17, Air)
/// have one; earlier models have the Ring/Silent switch. nil when it can't tell.
enum DeviceFeatures {
    static func hasActionButton(modelIdentifier: String) -> Bool? {
        guard modelIdentifier.hasPrefix("iPhone") else { return nil }
        let numbers = modelIdentifier.dropFirst("iPhone".count).split(separator: ",")
        guard numbers.count == 2, let major = Int(numbers[0]), Int(numbers[1]) != nil else { return nil }
        return major >= 16
    }

    /// This device's model identifier (the simulated one in the Simulator).
    static var modelIdentifier: String {
        if let simulated = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"], !simulated.isEmpty {
            return simulated
        }
        var info = utsname()
        uname(&info)
        return withUnsafeBytes(of: &info.machine) { buffer in
            String(decoding: buffer.prefix { $0 != 0 }, as: UTF8.self)
        }
    }

    /// nil when unknown (iPad, Mac, a model identifier iOS made up).
    static var thisPhoneHasActionButton: Bool? {
        hasActionButton(modelIdentifier: modelIdentifier)
    }
}
