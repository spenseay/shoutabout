import ActivityKit
import Foundation

/// Drives the dictation Live Activity (the mouth in the Dynamic Island / on the Lock Screen).
///
/// iOS only lets a background recording keep the microphone while a Live Activity is showing,
/// so `start` runs BEFORE the recorder. Voice-level updates are quantised and throttled
/// (`ActivityLevelThrottle`); while sending, a few saTalk frames a second make the mouth chew.
/// The activity itself is only touched from `LiveActivityBridge` (nonisolated), so no
/// non-Sendable ActivityKit object crosses actors.
@MainActor
final class LiveActivityController {
    private var activityID: String?
    private var state: DictationActivityState?
    private var throttle = ActivityLevelThrottle()
    private var updates: Task<Void, Never>?
    private var talking: Task<Void, Never>?
    private var ending: Task<Void, Never>?

    /// How long the zipped mouth ("Pasted · on your PC") stays in the island once sealed.
    static let doneLinger: Duration = .seconds(2)
    /// ...and on the Lock Screen after the activity ended (its final content: still zipped).
    static let doneDismissal: TimeInterval = 2
    /// "Copy ›" (the text waits for the app to be in front): long enough to tap it, short
    /// enough to stay inside the background time iOS gives the app.
    static let tapToCopyLinger: Duration = .seconds(8)
    static let failedLinger: Duration = .seconds(4)
    static let talkFrameInterval: Duration = .milliseconds(320)
    /// The zip, as stills the island animates between (0 = the slider pops in at the left of
    /// the open mouth, 1 = sealed), one every `zipStep`.
    static let zipStills: [Double] = [0, 0.5, 1]
    static let zipStep: Duration = .milliseconds(250)

    var isRunning: Bool { activityID != nil }

    /// Shows "Listening · to Computer". Returns false when iOS refuses (Live Activities off,
    /// too many activities, or the app isn't allowed to start one right now).
    func start(target: DictationTarget, computerName: String?, startedAt: Date) async -> Bool {
        cancelTimers()
        await LiveActivityBridge.endAll()
        let state = DictationActivityState(phase: .listening, target: target, computerName: computerName, startedAt: startedAt)
        guard LiveActivityBridge.areEnabled(), let id = LiveActivityBridge.request(state) else {
            activityID = nil
            self.state = nil
            return false
        }
        activityID = id
        self.state = state
        throttle.reset()
        return true
    }

    /// Voice level 0...1 from the meter (called ~20 times a second; sends only a few).
    func level(_ level: Double) {
        guard var state, state.phase == .listening else { return }
        let step = DictationActivityState.levelStep(for: level)
        guard throttle.shouldSend(step: step, at: Date()) else { return }
        state.level = step
        push(state)
    }

    /// Recording stopped: the mouth chews while the PC (or this iPhone) works.
    func sending() {
        guard var state else { return }
        state.phase = .sending
        state.level = 0
        push(state)
        talking?.cancel()
        talking = Task { [weak self] in
            var frame = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: Self.talkFrameInterval)
                guard let self, !Task.isCancelled, var current = self.state, current.phase == .sending else { return }
                frame += 1
                current.frame = frame
                self.push(current)
            }
        }
    }

    /// "Pasted" / "Copied" in the island while the mouth zips shut, then the activity ends with
    /// the zipped mouth. The returned task finishes when it has ended (callers keep the app
    /// alive until then). `tapToCopy`: the text isn't on the clipboard yet, so it says
    /// "Copy ›" a little longer (a tap on it opens the app, which copies it).
    @discardableResult
    func finish(outcome: DictationActivityState.Outcome, message: String, tapToCopy: Bool = false) -> Task<Void, Never>? {
        conclude(
            phase: .done,
            outcome: outcome,
            message: message,
            tapToCopy: tapToCopy,
            linger: tapToCopy ? Self.tapToCopyLinger : Self.doneLinger
        )
    }

    /// The failure reason, briefly, then the activity ends.
    @discardableResult
    func fail(message: String) -> Task<Void, Never>? {
        conclude(phase: .failed, outcome: nil, message: DictationActivityState.shortReason(message), tapToCopy: false, linger: Self.failedLinger)
    }

    /// The app copied the result after all (it came to the front): a "Copy ›" that's still up
    /// has done its job.
    func copied() {
        guard state?.tapToCopy == true else { return }
        Task { await cancel() }
    }

    /// Ends it now (recording cancelled or too short).
    func cancel() async {
        cancelTimers()
        let id = activityID
        activityID = nil
        state = nil
        await updates?.value
        await LiveActivityBridge.end(id: id, finalState: nil)
    }

    private func conclude(
        phase: DictationActivityState.Phase,
        outcome: DictationActivityState.Outcome?,
        message: String,
        tapToCopy: Bool,
        linger: Duration
    ) -> Task<Void, Never>? {
        talking?.cancel()
        talking = nil
        guard var state, let id = activityID else { return nil }
        state.phase = phase
        state.outcome = outcome
        state.message = message
        state.tapToCopy = tapToCopy ? true : nil
        state.level = 0
        // Delivered: the mouth zips shut, as a few stills (the island animates between them).
        let stills = state.zips ? Self.zipStills : []
        state.zip = stills.first
        push(state)
        let firstState = state
        ending = Task { [weak self] in
            var lastState = firstState
            for still in stills.dropFirst() {
                try? await Task.sleep(for: Self.zipStep)
                guard let self, !Task.isCancelled, self.activityID == id else { return }
                lastState.zip = still
                self.push(lastState)
            }
            await self?.updates?.value
            try? await Task.sleep(for: linger)
            guard let self, !Task.isCancelled, self.activityID == id else { return }
            self.activityID = nil
            self.state = nil
            // Ends on the sealed zipper, which stays on the Lock Screen a moment longer.
            let dismissAfter: TimeInterval? = lastState.zips ? Self.doneDismissal : nil
            await LiveActivityBridge.end(id: id, finalState: lastState, dismissAfter: dismissAfter)
        }
        return ending
    }

    private func push(_ newState: DictationActivityState) {
        state = newState
        guard let id = activityID else { return }
        let previous = updates
        updates = Task {
            await previous?.value
            await LiveActivityBridge.update(id: id, state: newState)
        }
    }

    private func cancelTimers() {
        talking?.cancel()
        talking = nil
        ending?.cancel()
        ending = nil
    }

    #if DEBUG
    /// Screenshot scenes: put a sample activity on screen (the app must be in the foreground).
    func showDemo(_ state: DictationActivityState) async {
        cancelTimers()
        await LiveActivityBridge.endAll()
        activityID = LiveActivityBridge.request(state)
        self.state = state
    }

    /// Screenshot scenes: an update with an alert, which makes iOS expand the island briefly.
    func alertDemo(_ state: DictationActivityState) async {
        guard let id = activityID else { return }
        self.state = state
        await LiveActivityBridge.updateWithAlert(id: id, state: state)
    }
    #endif
}

/// The only place that touches ActivityKit objects. Nonisolated, and only Sendable values
/// (ids and states) go in and out.
enum LiveActivityBridge {
    static func areEnabled() -> Bool {
        ActivityAuthorizationInfo().areActivitiesEnabled
    }

    static func request(_ state: DictationActivityState) -> String? {
        do {
            let activity = try Activity.request(
                attributes: DictationActivityAttributes(),
                content: ActivityContent(state: state, staleDate: nil),
                pushType: nil
            )
            return activity.id
        } catch {
            return nil
        }
    }

    static func update(id: String, state: DictationActivityState) async {
        for activity in Activity<DictationActivityAttributes>.activities where activity.id == id {
            await activity.update(ActivityContent(state: state, staleDate: nil))
        }
    }

    static func updateWithAlert(id: String, state: DictationActivityState) async {
        for activity in Activity<DictationActivityAttributes>.activities where activity.id == id {
            let alert = AlertConfiguration(
                title: LocalizedStringResource(stringLiteral: state.headline),
                body: LocalizedStringResource(stringLiteral: state.detail),
                sound: .default
            )
            await activity.update(ActivityContent(state: state, staleDate: nil), alertConfiguration: alert)
        }
    }

    /// Ends it, gone at once, or (`dismissAfter`) showing `finalState` on the Lock Screen for
    /// that many seconds more.
    static func end(id: String?, finalState: DictationActivityState?, dismissAfter: TimeInterval? = nil) async {
        guard let id else { return }
        var policy = ActivityUIDismissalPolicy.immediate
        if let dismissAfter, dismissAfter > 0 {
            policy = .after(Date().addingTimeInterval(dismissAfter))
        }
        for activity in Activity<DictationActivityAttributes>.activities where activity.id == id {
            let content = finalState.map { ActivityContent(state: $0, staleDate: nil) }
            await activity.end(content, dismissalPolicy: policy)
        }
    }

    /// Clears activities left by an earlier run (the app was killed mid-dictation).
    static func endAll() async {
        for activity in Activity<DictationActivityAttributes>.activities {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }
}
