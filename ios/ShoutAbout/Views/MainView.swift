import SwiftUI

enum MainSheet: String, Identifiable, Sendable {
    case history
    case settings

    var id: String { rawValue }
}

/// The one screen: Computer | Phone at the top (only with a paired computer), the big mouth
/// button in the middle, one status line, and the last result as a compact card that always
/// sits above the bottom safe area. Recent and Settings open as sheets.
///
/// Everything under the top bar is one big target: a tap anywhere starts recording, and while
/// recording a tap anywhere sends (the buttons in it, Cancel, Undo, Retry, Stop waiting and
/// the card's copy, only do their own thing). While recording the whole screen is red, with a
/// big timer, and Cancel is a large button at the bottom.
struct MainView: View {
    @Environment(AppModel.self) private var model
    @State private var sheet: MainSheet? = MainView.initialSheet

    var body: some View {
        GeometryReader { geo in
            let tile = max(150, min(geo.size.width - 96, geo.size.height * 0.40, 280))
            VStack(spacing: 0) {
                TopBar(sheet: $sheet)
                    .padding(.horizontal, Theme.Spacing.l)
                    .padding(.top, Theme.Spacing.s)
                VStack(spacing: 0) {
                    Spacer(minLength: Theme.Spacing.m)
                    MouthButton()
                        .frame(width: tile, height: tile)
                    StatusBlock()
                        .padding(.top, Theme.Spacing.xl)
                        .padding(.horizontal, Theme.Spacing.xl)
                    Spacer(minLength: Theme.Spacing.m)
                    BottomArea()
                        .padding(.horizontal, Theme.Spacing.l)
                        .padding(.bottom, Theme.Spacing.m)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .contentShape(Rectangle())
                .onTapGesture { model.toggleRecording() }
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .background(ground.ignoresSafeArea())
        .animation(.spring(duration: 0.35), value: model.phase)
        .animation(.spring(duration: 0.35), value: model.discarded)
        .sheet(item: $sheet) { which in
            switch which {
            case .history:
                HistoryView()
            case .settings:
                SettingsView()
            }
        }
        .sensoryFeedback(.impact(weight: .heavy), trigger: model.startTick)
        .sensoryFeedback(.success, trigger: model.successTick)
        .sensoryFeedback(.warning, trigger: model.warningTick)
        .sensoryFeedback(.error, trigger: model.failureTick)
    }

    private var ground: Color {
        model.phase == .recording ? Theme.Colors.groundRecording : Theme.Colors.ground
    }

    private nonisolated static var initialSheet: MainSheet? {
#if DEBUG
        return ScreenshotScene.current?.sheet
#else
        return nil
#endif
    }
}

// MARK: - Top bar

private struct TopBar: View {
    @Environment(AppModel.self) private var model
    @Binding var sheet: MainSheet?

    var body: some View {
        HStack(spacing: Theme.Spacing.xs) {
            CircleIconButton(systemImage: "clock.arrow.circlepath", label: "Recent") { sheet = .history }
            Spacer(minLength: 0)
            if model.hasComputer {
                TargetPill()
                    .layoutPriority(1)
            }
            Spacer(minLength: 0)
            CircleIconButton(systemImage: "gearshape.fill", label: "Settings") { sheet = .settings }
        }
        .frame(height: 48)
    }
}

struct CircleIconButton: View {
    var systemImage: String
    var label: String
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 17, weight: .bold))
                .foregroundStyle(Theme.Colors.ink)
                .frame(width: 44, height: 44)
                .overlay(Circle().stroke(Theme.Colors.ink, lineWidth: 2.5))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// Computer | Phone. The computer side shows the PC's name.
private struct TargetPill: View {
    @Environment(AppModel.self) private var model
    @Namespace private var pill

    var body: some View {
        HStack(spacing: 0) {
            segment(.pc, title: model.computerName ?? "Computer")
                .layoutPriority(1)
            segment(.phone, title: "Phone")
                .fixedSize()
        }
        .padding(3)
        .overlay(Capsule().stroke(Theme.Colors.ink, lineWidth: 2.5))
        .sensoryFeedback(.selection, trigger: model.target)
        // Not `.disabled`: that would grey the switch out while recording.
        .allowsHitTesting(model.phase != .recording && !model.phase.isBusy)
    }

    private func segment(_ option: DictationTarget, title: String) -> some View {
        let selected = model.target == option
        return Button {
            withAnimation(.spring(duration: 0.3, bounce: 0.35)) { model.target = option }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: option.systemImage)
                Text(title)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .font(Theme.Typography.bold(13))
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
            .frame(maxWidth: 190)
            .foregroundStyle(selected ? Theme.Colors.pillOnInk : Theme.Colors.pillOffInk)
            .background {
                if selected {
                    Capsule()
                        .fill(Theme.Colors.pillOn)
                        .matchedGeometryEffect(id: "pill", in: pill)
                }
            }
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(option == .pc ? "Type on \(title)" : "Copy on this phone")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

// MARK: - The mouth button

/// The app icon as a button: a yellow tile with the primary mark (no ring) and a thin ink edge.
/// Idle: mouth ajar, breathing. Recording: the mouth follows the mic level and punches on loud
/// peaks. Processing: saTalk + shimmer. Done: punch, and when the words landed (pasted or
/// copied) the mouth zips shut, then it's idle again. Failed: red tile, shake.
struct MouthButton: View {
    @Environment(AppModel.self) private var model

    static func mouthRatio(for level: Double) -> CGFloat {
        let open = Theme.Motion.micMinOpen + (Theme.Motion.micMaxOpen - Theme.Motion.micMinOpen) * CGFloat(level)
        return Theme.Mouth.openRatio * open
    }

    /// The words landed (pasted or copied): the mouth zips shut. Not when nothing was heard.
    static func zips(_ item: HistoryItem?) -> Bool {
        guard let item else { return false }
        return item.succeeded && !item.text.isEmpty
    }

    var body: some View {
        // The zip starts the moment the words land, not after a cross-fade from the talking mouth.
        let zipping = model.phase == .done && Self.zips(model.lastItem)
        Button {
            model.toggleRecording()
        } label: {
            GeometryReader { geo in
                let side = min(geo.size.width, geo.size.height)
                let radius = side * Theme.Radius.tileFraction
                ZStack {
                    if model.phase == .recording {
                        RoundedRectangle(cornerRadius: radius * 1.1, style: .continuous)
                            .stroke(Theme.Colors.recordingStroke, lineWidth: 6)
                            .padding(-12)
                            .scaleEffect(1 + 0.05 * model.level)
                            .opacity(0.45 + 0.55 * model.level)
                    }
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .fill(tileColor)
                    mouth
                        .frame(width: side * Theme.Mouth.tileFraction)
                        .transaction { transaction in
                            if zipping { transaction.animation = nil }
                        }
                        // ...while it still moves with the tile (the result card pushes it up).
                        .geometryGroup()
                        // The zip's pull swings out past the mouth: it stays inside the tile.
                        .frame(width: side, height: side)
                        .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
                    if model.phase.isBusy {
                        Shimmer(cornerRadius: radius)
                    }
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(Theme.Colors.buttonStroke, lineWidth: Theme.Size.borderWidth)
                }
                .frame(width: side, height: side)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .aspectRatio(1, contentMode: .fit)
        }
        .buttonStyle(SquishButtonStyle())
        .disabled(model.phase.isBusy)
        .shoutPunch(trigger: model.successTick, strength: 1.6)
        .shake(trigger: model.failureTick)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityHint("Starts or stops recording")
        .accessibilityIdentifier("mouthButton")
    }

    private var mouthStyle: Theme.MouthStyle { .standard }

    @ViewBuilder
    private var mouth: some View {
        switch model.phase {
        case .idle:
            BreathingMouth(style: mouthStyle)
        case .recording:
            MouthView(ratio: Self.mouthRatio(for: model.level), tongue: true, style: mouthStyle)
                .animation(.easeOut(duration: 0.08), value: model.level)
                .shoutPunch(trigger: model.peakTick, strength: 1.2)
        case .processing:
            TalkingMouthView(style: mouthStyle)
        case .done:
            if Self.zips(model.lastItem) {
                ZipFinishMouth(style: mouthStyle)
            } else {
                MouthView(ratio: Theme.Mouth.openRatio, tongue: true, style: mouthStyle)
            }
        case .failed:
            MouthView(ratio: Theme.Mouth.ajarRatio, tongue: false, style: mouthStyle)
        }
    }

    private var tileColor: Color {
        if case .failed = model.phase { return Theme.Colors.tileError }
        return Theme.Colors.button
    }

    private var accessibilityLabel: String {
        switch model.phase {
        case .recording: return "Stop and send"
        case .processing: return "Working"
        default: return "Start dictating"
        }
    }
}

// BreathingMouth and ZipFinishMouth: Shared/UI/MouthMotion.swift (the keyboard draws them too).

/// A diagonal light sweep across the tile while waiting.
private struct Shimmer: View {
    var cornerRadius: CGFloat

    var body: some View {
        Group {
            if MotionPolicy.frozen {
                band(progress: 0.55)
            } else {
                TimelineView(.animation) { context in
                    let period = 1.4
                    let t = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: period) / period
                    band(progress: t)
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        .allowsHitTesting(false)
    }

    private func band(progress: Double) -> some View {
        GeometryReader { geo in
            let width = geo.size.width
            LinearGradient(
                colors: [.clear, Theme.Colors.buttonShimmer, .clear],
                startPoint: .leading,
                endPoint: .trailing
            )
            .frame(width: width * 0.45)
            .rotationEffect(.degrees(18))
            .offset(x: -width * 0.6 + CGFloat(progress) * width * 1.7)
        }
    }
}

struct SquishButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(.spring(duration: 0.2, bounce: 0.5), value: configuration.isPressed)
    }
}

// MARK: - Status

private struct StatusBlock: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let recording = model.phase == .recording
        VStack(spacing: Theme.Spacing.s) {
            Text(title)
                .shoutHeadline(recording ? 64 : 36)
                .multilineTextAlignment(.center)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .contentTransition(.numericText())
                .accessibilityIdentifier("statusTitle")
            if let subtitle {
                Text(subtitle)
                    .font(Theme.Typography.body(15))
                    .foregroundStyle(recording ? Theme.Colors.inkMutedRecording : Theme.Colors.inkMuted)
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: Theme.Spacing.m) {
                if model.phase.isBusy {
                    TextActionButton(title: "Stop waiting", role: .cancel) { model.cancelProcessing() }
                }
                if model.canRetry {
                    TextActionButton(title: "Retry") { model.retryLast() }
                }
            }
            .font(Theme.Typography.bold(16))
            .foregroundStyle(Theme.Colors.ink)
        }
        .frame(maxWidth: .infinity)
    }

    private var title: String {
        switch model.phase {
        case .idle:
            return model.discarded != nil ? "Discarded" : "Tap anywhere & talk"
        case .recording:
            return Duration.seconds(model.elapsed).formatted(.time(pattern: .minuteSecond))
        case .processing:
            return "Hold on…"
        case .done:
            guard let item = model.lastItem else { return "Done" }
            if item.text.isEmpty { return "Nothing heard" }
            if item.pasted { return "Pasted!" }
            return model.clipboardPending ? "Ready!" : "Copied!"
        case .failed:
            return "Didn't land"
        }
    }

    private var subtitle: String? {
        switch model.phase {
        case .idle:
            if model.discarded != nil { return nil }
            if model.effectiveTarget == .pc {
                return "Types on \(model.computerDisplayName)"
            }
            return "Copies to your clipboard"
        case .recording:
            return "Tap anywhere to send"
        case .processing(let status):
            return status
        case .done:
            guard let item = model.lastItem else { return nil }
            if item.text.isEmpty { return "Try again a little louder." }
            if model.clipboardPending, !item.pasted { return "Tap copy to put it on your clipboard." }
            if let warning = item.warning { return warning }
            return item.pasted ? "On \(model.computerDisplayName)" : nil
        case .failed(let message):
            return message
        }
    }
}

/// A text button with room around it, so a near miss still hits it rather than the screen
/// behind it (which would start recording).
private struct TextActionButton: View {
    var title: String
    var role: ButtonRole?
    var action: () -> Void

    init(title: String, role: ButtonRole? = nil, action: @escaping () -> Void) {
        self.title = title
        self.role = role
        self.action = action
    }

    var body: some View {
        Button(role: role, action: action) {
            Text(title)
                .padding(.horizontal, Theme.Spacing.m)
                .padding(.vertical, Theme.Spacing.s)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Bottom: Cancel, Undo, or the result

/// Where the thumb is: Cancel while recording (a mis-tap is undone with Undo, in the same
/// spot), Undo for a few seconds after Cancel, otherwise the last result.
private struct BottomArea: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if model.phase == .recording {
            CapsuleActionButton(title: "Cancel", systemImage: "xmark") { model.cancelRecording() }
                .accessibilityIdentifier("cancelButton")
                .transition(.opacity)
        } else if model.phase == .idle, model.discarded != nil {
            CapsuleActionButton(title: "Undo", systemImage: "arrow.uturn.backward") { model.undoCancel() }
                .accessibilityHint("Sends the recording you cancelled")
                .accessibilityIdentifier("undoButton")
                .transition(.opacity)
        } else if model.phase == .done, let item = model.lastItem, item.succeeded {
            ResultCard(item: item)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}

/// A large outlined capsule with an icon (Cancel, Undo): easy to hit, and clearly not part of
/// "tap anywhere".
private struct CapsuleActionButton: View {
    var title: String
    var systemImage: String
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(Theme.Typography.bold(18))
                .foregroundStyle(Theme.Colors.ink)
                .padding(.horizontal, Theme.Spacing.xxl)
                .frame(minWidth: 200, minHeight: 56)
                .overlay(Capsule().strokeBorder(Theme.Colors.ink, lineWidth: Theme.Size.borderWidth))
                .contentShape(Capsule())
        }
        .buttonStyle(SquishButtonStyle())
    }
}

/// The last result, compact: the first lines (Recent has the rest) and a copy button.
struct ResultCard: View {
    @Environment(AppModel.self) private var model
    let item: HistoryItem

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Text(item.text.isEmpty ? (item.warning ?? "No speech heard.") : item.text)
                    .font(Theme.Typography.body(16))
                    .foregroundStyle(Theme.Colors.cardInk)
                    .lineLimit(3)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 0)
            if !item.text.isEmpty {
                Button {
                    model.copy(item)
                } label: {
                    Image(systemName: "doc.on.doc")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(Theme.Colors.pillOnInk)
                        .frame(width: 44, height: 44)
                        .background(Circle().fill(Theme.Colors.pillOn))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Copy again")
            }
        }
        .padding(Theme.Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous).fill(Theme.Colors.card))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                .stroke(Theme.Colors.ink, lineWidth: Theme.Size.borderWidth)
        )
        .accessibilityIdentifier("resultCard")
    }
}
