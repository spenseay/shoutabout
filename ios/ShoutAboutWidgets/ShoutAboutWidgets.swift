import AppIntents
import SwiftUI
import WidgetKit

@main
struct ShoutAboutWidgetsBundle: WidgetBundle {
    var body: some Widget {
        TargetControl()
        DictateControl()
        DictateLockScreenWidget()
        DictationLiveActivity()
    }
}

// MARK: - Control Center: Computer / Phone toggle

/// Control Center toggle: on = Phone (copy on the iPhone), off = Computer (type on the PC).
struct TargetControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: WidgetKinds.targetControl, provider: TargetValueProvider()) { isPhone in
            ControlWidgetToggle("Dictation Target", isOn: isPhone, action: SetPhoneModeIntent()) { isOn in
                Label(isOn ? "Phone" : "Computer", systemImage: isOn ? "iphone" : "desktopcomputer")
            }
            .tint(Theme.Palette.tongueRed)
        }
        .displayName("Dictation Target")
        .description("Type dictation on your computer, or copy it on this phone.")
    }
}

struct TargetValueProvider: ControlValueProvider {
    var previewValue: Bool { false }

    func currentValue() async throws -> Bool {
        SharedSettings().target == .phone
    }
}

// MARK: - Control Center / Action Button: dictate

/// Control Center button (also pickable for the Action Button under Controls): records
/// without opening the app. DictateIntent is an AudioRecordingIntent + LiveActivityIntent, so
/// iOS runs it in the app's process, which starts the Live Activity and the microphone.
struct DictateControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: WidgetKinds.dictateControl) {
            ControlWidgetButton(action: DictateIntent()) {
                Label("Dictate", systemImage: "mouth.fill")
            }
            .tint(Theme.Palette.tongueRed)
        }
        .displayName("Dictate")
        .description("Talk without opening ShoutAbout. Press again to send.")
    }
}

// MARK: - Lock Screen widget

struct TargetEntry: TimelineEntry {
    let date: Date
    let target: DictationTarget
}

struct TargetTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> TargetEntry {
        TargetEntry(date: Date(), target: .pc)
    }

    func getSnapshot(in context: Context, completion: @escaping (TargetEntry) -> Void) {
        completion(TargetEntry(date: Date(), target: SharedSettings().target))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TargetEntry>) -> Void) {
        let entry = TargetEntry(date: Date(), target: SharedSettings().target)
        completion(Timeline(entries: [entry], policy: .never))
    }
}

/// Lock Screen / StandBy accessory: the mouth mark (circular) or mark + target (rectangular).
/// Tapping opens shoutabout://dictate, which starts recording in the app.
struct DictateLockScreenWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: WidgetKinds.lockScreen, provider: TargetTimelineProvider()) { entry in
            DictateWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    AccessoryWidgetBackground()
                }
                .widgetURL(URL(string: "shoutabout://dictate"))
        }
        .configurationDisplayName("Shout")
        .description("Tap to start dictating.")
        .supportedFamilies([.accessoryCircular, .accessoryRectangular, .accessoryInline])
    }
}

struct DictateWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: TargetEntry

    var body: some View {
        switch family {
        case .accessoryRectangular:
            HStack(spacing: 8) {
                MouthView(ratio: Theme.Mouth.openRatio, tongue: true, style: .standard)
                    .frame(width: 38)
                    .widgetAccentable()
                VStack(alignment: .leading, spacing: 0) {
                    Text("SHOUT")
                        .font(Theme.Typography.display(20))
                    Text("to \(entry.target.label)")
                        .font(.caption)
                }
            }
        case .accessoryInline:
            Label("Shout to \(entry.target.label)", systemImage: "mouth.fill")
        default:
            MouthView(ratio: Theme.Mouth.openRatio, tongue: true, style: .standard)
                .padding(9)
                .widgetAccentable()
        }
    }
}
