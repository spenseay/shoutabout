import ActivityKit
import Foundation

/// The dictation Live Activity: started when a recording starts (from the Action Button,
/// Control Center or the app), drawn by the widget extension (`DictationLiveActivity`).
/// Everything that changes lives in `DictationActivityState` (Shared/Core, unit-tested).
struct DictationActivityAttributes: ActivityAttributes {
    typealias ContentState = DictationActivityState

    /// Static data. Bumped if the state's shape ever changes incompatibly.
    var schema: Int = 1
}
