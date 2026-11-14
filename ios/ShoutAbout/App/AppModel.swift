import Observation
import SwiftUI
import UIKit
import WidgetKit

// The shared intents call DictationLauncher only when SHOUTABOUT_APP is set (project.yml);
// without it the Action Button would silently fall back to the widget-extension path.
#if !SHOUTABOUT_APP
#error("SHOUTABOUT_APP must be in SWIFT_ACTIVE_COMPILATION_CONDITIONS for the app target")
#endif

enum DictationPhase: Equatable {
    case idle
    case recording
    /// Waiting for the PC (or the on-device model); the string is the status line.
    case processing(String)
    case done
    case failed(String)

    var isBusy: Bool {
        if case .processing = self { return true }
        return false
    }
}

struct ConnectionCheck: Identifiable, Equatable, Sendable {
    var id = UUID()
    var title: String
    var ok: Bool
    var detail: String
}

/// Everything a dictation needs, captured when recording stops (later settings edits don't
/// change an upload in flight, and Retry reuses the audio).
struct DictationJob: Sendable {
    var recording: Recording
    var target: DictationTarget
    var computer: ComputerLink?
    var cachedInfo: HubInfo?
    var copyInPCMode: Bool
    var offlineEngine: OfflineEnginePreference
    var vocab: [String]
    /// Streamed to the PC while it was recorded (`StreamingUploader`); the recording's file is
    /// the fallback. Never kept for Retry.
    var stream: StreamingUploader? = nil
}

/// A recording thrown away with Cancel, kept a few seconds for Undo.
struct DiscardedRecording: Equatable, Sendable {
    var recording: Recording
    var target: DictationTarget
}

enum StartResult: Equatable, Sendable {
    case started
    case busy
    /// Only the foreground can do it (ask for the microphone, or Live Activities are off).
    case needsForeground(String)
    case failed(String)
}

/// App state: settings (mirrored to the App Group), the paired computer, the dictation state
/// machine with its Live Activity, Recent and vocabulary. Views bind to it; intents reach it
/// through `DictationLauncher`.
@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    // MARK: Settings

    var target: DictationTarget = .pc {
        didSet {
            guard oldValue != target, !isDemo else { return }
            settings.target = target
            ControlCenter.shared.reloadControls(ofKind: WidgetKinds.targetControl)
            WidgetCenter.shared.reloadTimelines(ofKind: WidgetKinds.lockScreen)
        }
    }
    var copyInPCMode = false { didSet { if !isDemo { settings.copyInPCMode = copyInPCMode } } }
    var useTunnel = true { didSet { if !isDemo { settings.useTunnel = useTunnel } } }
    var offlineEngine: OfflineEnginePreference = .automatic {
        didSet {
            guard !isDemo else { return }
            settings.offlineEngine = offlineEngine
            Task { await refreshOfflineStatus() }
        }
    }
    private(set) var onboardingDone = false

    // MARK: The computers

    /// v2 pairing (Bonjour + Allow on the PC): the computers, the selected one ("Computer"
    /// always means it), and which one has the code pairing.
    private(set) var roster = ComputerRoster()
    /// Legacy QR / manual pairing (LAN URL, public URL, Wi-Fi name).
    private(set) var configuration = HubConfiguration()
    /// The computers whose device token is in the keychain.
    private(set) var computersWithToken: Set<String> = []
    private(set) var hasLegacyToken = false
    private(set) var hasCloudflareToken = false
    /// Each computer's last `/info` (speed, name), by hub key.
    private(set) var hubInfos: [CachedHubInfo] = []
    /// Goes up with every pairing change (the connect screen tells a finished code pairing by it).
    private(set) var pairingChanges = 0
    private(set) var vocab: [String] = []
    private(set) var vocabFetchedAt: Date?
    private(set) var loudIcon = false

    // MARK: Dictation state

    private(set) var phase: DictationPhase = .idle {
        didSet {
            keyboardPhaseChanged(from: oldValue)
            if oldValue.isBusy, !phase.isBusy { resumeDeliveryWaiters() }
        }
    }
    /// Smoothed mic level 0...1 while recording (drives the mouth).
    private(set) var level: Double = 0
    private(set) var elapsed: TimeInterval = 0
    private(set) var lastItem: HistoryItem?
    /// Recent: the last few dictations, in memory only (never saved, gone when the app quits).
    private(set) var history = HistoryStore()
    private(set) var lastFailedJob: DictationJob?
    /// Where the current recording goes (fixed when it starts).
    private(set) var recordingTarget: DictationTarget?
    /// The last result isn't on the clipboard: it finished in the background (iOS doesn't let
    /// the app copy there) and waits for the app to be in front, or that took too long and
    /// the Copy button has to do it (see `putOnClipboard`).
    private(set) var clipboardPending = false
    /// Cancel keeps the recording `undoWindow` for Undo ("Discarded · Undo"): a mis-tap on
    /// Cancel mustn't lose minutes of talking.
    private(set) var discarded: DiscardedRecording?

    // MARK: The ShoutAbout keyboard

    /// The keyboard has come up with Full Access (Settings › Keyboard: On).
    private(set) var keyboardOn = false
    /// The keyboard opened the app to start listening: the screen says to go back.
    private(set) var keyboardListeningScreen = false

    // Triggers for haptics and animations (views watch the counters).
    private(set) var startTick = 0
    private(set) var successTick = 0
    private(set) var warningTick = 0
    private(set) var failureTick = 0
    private(set) var peakTick = 0

    // MARK: Settings screen state

    private(set) var connectionChecks: [ConnectionCheck] = []
    private(set) var isCheckingConnection = false
    private(set) var offlineModelStatus = ""
    private(set) var isPreparingOffline = false
    var vocabMessage: String?

    // MARK: Private

    @ObservationIgnored private let settings = SharedSettings()
    @ObservationIgnored private let keychain = KeychainStore()
    @ObservationIgnored private let recorder = AudioRecorder()
    @ObservationIgnored let activity = LiveActivityController()
    @ObservationIgnored private let finder = ComputerFinder()
    @ObservationIgnored private var meterTask: Task<Void, Never>?
    @ObservationIgnored private var processingTask: Task<Void, Never>?
    @ObservationIgnored private var sceneActive = false
    @ObservationIgnored private var pendingStart = false
    @ObservationIgnored private var isStarting = false
    @ObservationIgnored private var backgroundTaskID: UIBackgroundTaskIdentifier = .invalid
    @ObservationIgnored private var lastRefresh: Date?
    @ObservationIgnored private var vocabRefreshTask: Task<Void, Never>?
    @ObservationIgnored private var isDemo = false
    @ObservationIgnored private var demoEffectsDone = false
    @ObservationIgnored private let keyboardLink = KeyboardLinkStore()
    @ObservationIgnored private var keyboardTicker: Task<Void, Never>?
    @ObservationIgnored private var keyboardSessionLastUse = Date()
    @ObservationIgnored private var keyboardPublishedPhase: KeyboardAppState.Phase = .idle
    @ObservationIgnored private var keyboardPhaseSince = Date()
    @ObservationIgnored private var keyboardStarting = false
    @ObservationIgnored private var keyboardStopRequested = false
    @ObservationIgnored private var keyboardOpenPending = false
    /// The recording in progress, streamed to the PC meanwhile (nil: not streamed).
    @ObservationIgnored private var stream: StreamingUploader?
    @ObservationIgnored private var discardTask: Task<Void, Never>?
    /// Intents waiting for the dictation being sent to land (`waitForDelivery`).
    @ObservationIgnored private var deliveryWaiters: [CheckedContinuation<Void, Never>] = []
    /// Did the keyboard on screen type the last phone dictation (and so copy it)?
    @ObservationIgnored private var keyboardTypingWatch: Task<Bool, Never>?

    static let undoWindow: Duration = .seconds(8)

    private init() {
        // Once, from the version with a single computer: it keeps working, no pairing again.
        let keychain = self.keychain
        settings.migrateSingleComputer(hasCodePairing: settings.configuration.isPaired || keychain.secrets() != nil) {
            keychain.moveSingleDeviceToken(to: $0)
        }
        target = settings.target
        copyInPCMode = settings.copyInPCMode
        useTunnel = settings.useTunnel
        offlineEngine = settings.offlineEngine
        configuration = settings.configuration
        roster = settings.roster
        hubInfos = settings.cachedHubInfos
        vocab = settings.vocabTerms
        vocabFetchedAt = settings.vocabFetchedAt
        refreshSecretsSummary()
        // People who paired with the QR code before onboarding existed skip it.
        onboardingDone = settings.onboardingDone || (configuration.isPaired && hasLegacyToken)
#if DEBUG
        if let scene = ScreenshotScene.current {
            isDemo = true
            applyScreenshotScene(scene)
        }
#endif
        if !isDemo {
            // Older versions could save up to 200 dictations on disk; Recent never does.
            HistoryStore.removeSavedHistory(defaults: settings.defaults)
            setUpKeyboardLink()
        }
    }

    // MARK: Derived

    /// The selected computer (v2 pairing), if any. "Computer" means this one everywhere.
    var pairedComputer: PairedComputer? { roster.selected }

    /// The selected computer can be dictated to: it has a token on this iPhone (or the code
    /// pairing alone stands in, with no v2 computer paired).
    var hasComputer: Bool {
        if let selected = pairedComputer { return usableComputerIDs.contains(selected.id) }
        return codePairingIsTheComputer
    }

    /// Where a new recording goes: Phone when no computer is paired.
    var effectiveTarget: DictationTarget { hasComputer ? target : .phone }

    /// The selected computer's short name ("Desktop"), for the switch and the Live Activity.
    var computerName: String? {
        if let selected = pairedComputer { return roster.displayName(for: selected.id) }
        return hubInfo?.info.name.nonBlank.map { ComputerNames.pretty($0) }
    }

    /// For sentences: "Desktop" or "your PC".
    var computerDisplayName: String { computerName ?? "your PC" }

    /// The selected computer's last `/info` (speed, name).
    var hubInfo: CachedHubInfo? {
        guard let key = selectedHubKey else { return nil }
        return hubInfos.first { $0.hubKey == key }
    }

    /// Every paired computer with a short, distinct name (the code pairing when it's the only
    /// one), for Settings and the main screen's switch.
    var computerChoices: [ComputerChoice] {
        guard roster.computers.isEmpty else { return roster.choices(usable: usableComputerIDs) }
        guard codePairingIsTheComputer else { return [] }
        let reported = hubInfo?.info.name.nonBlank
        return [ComputerChoice(
            id: ComputerRoster.codePairingID,
            name: reported.map { ComputerNames.pretty($0) } ?? "Your PC",
            reportedName: reported ?? "",
            usable: true,
            isCodePairing: true
        )]
    }

    /// The id of the computer "Computer" means (`ComputerRoster.codePairingID` when the code
    /// pairing stands in).
    var selectedChoiceID: String? {
        if let selected = pairedComputer { return selected.id }
        return codePairingIsTheComputer ? ComputerRoster.codePairingID : nil
    }

    /// The computer can't be switched while a dictation records or is on its way.
    var computerSwitchLocked: Bool { phase == .recording || phase.isBusy }

    /// Computers with a token: their own, or the code pairing's when it is theirs.
    private var usableComputerIDs: Set<String> {
        var ids = computersWithToken
        if hasLegacyToken, let owner = roster.codePairingOwnerID { ids.insert(owner) }
        return ids
    }

    /// No v2 computer: the code pairing (QR) is the computer, as before v2.
    private var codePairingIsTheComputer: Bool {
        roster.computers.isEmpty && configuration.isPaired && hasLegacyToken
    }

    /// The `/info` cache key of the computer in use (`ComputerLink.hubKey`, without the keychain).
    private var selectedHubKey: String? {
        if let selected = pairedComputer { return selected.id }
        return roster.computers.isEmpty ? configuration.lanURL?.absoluteString : nil
    }

    var canRetry: Bool { lastFailedJob != nil && phase != .recording && !phase.isBusy }

    var hasLegacyPairing: Bool { configuration.isPaired || hasLegacyToken }

    // MARK: Commands from intents, URLs, the scene

    /// In-app intent / shoutabout://dictate / Lock Screen widget: start, or stop and send.
    func handleDictateCommand() {
        switch phase {
        case .recording:
            stopAndSend()
        case .processing:
            return
        default:
            startWhenActive()
        }
    }

    /// Starts recording now if the app is in the foreground, otherwise as soon as it is.
    func startWhenActive() {
        guard phase != .recording, !phase.isBusy else { return }
        guard sceneActive else {
            pendingStart = true
            return
        }
        Task { _ = await startRecording() }
    }

    func handle(url: URL) {
        guard url.scheme == "shoutabout" else { return }
        switch url.host() {
        case "dictate", "record", "start":
            handleDictateCommand()
        case "toggle-target":
            target = target.toggled
        case "copy":
            applyPendingCopy()
        case "keyboard":
            openedFromKeyboard()
        default:
            break  // "open" (the Live Activity) brings the app forward, which copies a waiting result
        }
    }

    func scenePhaseChanged(_ newPhase: ScenePhase) {
        sceneActive = newPhase == .active
        guard newPhase == .active else { return }
#if DEBUG
        if isDemo {
            Task { await runScreenshotSceneEffects() }
            return
        }
#endif
        reloadSharedState()
        // First thing in front: a result that finished in the background goes on the clipboard.
        applyPendingCopy()
        if PendingDictation.consume(in: settings) { pendingStart = true }
        if pendingStart {
            pendingStart = false
            handleDictateCommand()
        }
        if keyboardOpenPending {
            keyboardOpenPending = false
            Task { await startForKeyboard(showScreen: true) }
        }
        Task { await refreshComputer() }
    }

    /// Re-reads values other processes may have changed (Control Center toggle, intents, the
    /// keyboard).
    func reloadSharedState() {
        let stored = settings.target
        if stored != target { target = stored }
        guard !isDemo else { return }
        let on = keyboardLink.keyboardIsSetUp
        if on != keyboardOn { keyboardOn = on }
        _ = keyboardLink.handoff()  // removes one past its few minutes
    }

    // MARK: Onboarding

    func finishOnboarding(target newTarget: DictationTarget? = nil) {
        if let newTarget { target = newTarget }
        if !hasComputer { target = .phone }
        onboardingDone = true
        if !isDemo { settings.onboardingDone = true }
    }

    func showOnboardingAgain() {
        onboardingDone = false
        if !isDemo { settings.onboardingDone = false }
    }

    // MARK: Recording

    /// The big button.
    func toggleRecording() {
        switch phase {
        case .recording: stopAndSend()
        case .processing: return
        default: Task { _ = await startRecording() }
        }
    }

    /// Starts recording (in-app button, Action Button, Control Center, the keyboard). From the
    /// background the Live Activity must start first: iOS stops a background recording without
    /// one, unless the keyboard session's input is already running (`startForKeyboard`).
    /// The keyboard's dictations go to the phone (the field it types in), whatever the target,
    /// and need no Live Activity: the keyboard shows them.
    func startRecording(forKeyboard: Bool = false) async -> StartResult {
        guard phase != .recording, !phase.isBusy, !isStarting else { return .busy }
        isStarting = true
        defer { isStarting = false }
        let active = UIApplication.shared.applicationState == .active

        switch AudioRecorder.permission {
        case .granted:
            break
        case .undetermined:
            guard active else { return .needsForeground("Open ShoutAbout once to allow the microphone.") }
            guard await AudioRecorder.requestPermission() else { return microphoneDenied() }
        case .denied:
            return microphoneDenied()
        }

        let target = forKeyboard ? DictationTarget.phone : effectiveTarget
        let startedAt = Date()
        if forKeyboard {
            // One from just before, still showing its result: done.
            if activity.isRunning { await activity.cancel() }
        } else {
            let live = await activity.start(
                target: target,
                computerName: target == .pc ? computerName : nil,
                startedAt: startedAt
            )
            if !live, UIApplication.shared.applicationState != .active, !recorder.isLiveInputRunning {
                return .needsForeground("Turn on Live Activities in Settings › ShoutAbout.")
            }
        }
        guard phase != .recording, !phase.isBusy else {
            await activity.cancel()
            return .busy
        }
        do {
            try recorder.start()
        } catch {
            await activity.cancel()
            let message = error.localizedDescription
            showFailure(message)
            return .failed(message)
        }
        if !forKeyboard { startStreamIfUseful(target: target) }
        recordingTarget = target
        level = 0
        elapsed = 0
        phase = .recording
        startTick += 1
        startMetering()
        dropDiscarded()  // a new recording ends the Undo of a cancelled one
        discardPendingCopy()  // a new dictation supersedes one still waiting for the clipboard
        if !isDemo { keyboardLink.removeHandoff() }  // ...or for the keyboard
        refreshVocabQuietly()  // ready by the time you stop talking
        return .started
    }

    /// The recorder just started: when the PC transcribes while you talk (`StreamPolicy`), the
    /// audio streams to it meanwhile, from an input tap of its own. Not for the keyboard (its
    /// session's input already runs) or in the screenshot scenes.
    private func startStreamIfUseful(target: DictationTarget) {
        takeStream()?.cancel()
        guard !isDemo, let link = computerLink(),
              let endpoint = StreamPolicy.endpoint(
                  for: link,
                  info: freshInfo(for: link),
                  keyboardSession: recorder.isLiveInputRunning
              ),
              let uploader = StreamingUploader.start(target: target, endpoint: endpoint, hubID: link.hubID)
        else { return }
        guard recorder.isRecording else {
            uploader.cancel()  // the recording (the safe copy) comes first
            return
        }
        stream = uploader
    }

    /// The stream's input stops (before the recorder closes the audio session); the stream goes
    /// with the job, or is cancelled.
    private func takeStream() -> StreamingUploader? {
        let taken = stream
        stream = nil
        taken?.stopCapture()
        return taken
    }

    private func microphoneDenied() -> StartResult {
        let message = "Microphone access is off. Turn it on in Settings › ShoutAbout."
        showFailure(message)
        return .failed(message)
    }

    /// Stops and sends. The returned task ends when the dictation is delivered and the Live
    /// Activity is gone (intents await it so iOS keeps the app running until then).
    @discardableResult
    func stopAndSend() -> Task<Void, Never>? {
        guard phase == .recording else { return nil }
        stopMetering()
        let stream = takeStream()
        let activity = self.activity
        guard let recording = recorder.stop() else {
            stream?.cancel()
            phase = .idle
            return Task { await activity.cancel() }
        }
        guard recording.duration >= 0.3 else {
            stream?.cancel()
            try? FileManager.default.removeItem(at: recording.url)
            phase = .idle
            return Task { await activity.cancel() }
        }
        return run(makeJob(recording, stream: stream))
    }

    /// Cancel: nothing is sent, but the recording is kept `undoWindow` for Undo.
    func cancelRecording() {
        guard phase == .recording else { return }
        stopMetering()
        takeStream()?.cancel()
        let target = recordingTarget ?? effectiveTarget
        let recording = recorder.stop()
        phase = .idle
        let activity = self.activity
        Task { await activity.cancel() }
        guard let recording else { return }
        guard recording.duration >= 0.3 else {
            try? FileManager.default.removeItem(at: recording.url)
            return
        }
        keepForUndo(DiscardedRecording(recording: recording, target: target))
    }

    /// Undo after Cancel: sends the recording as if Stop had been pressed.
    func undoCancel() {
        guard let item = discarded, phase != .recording, !phase.isBusy else { return }
        discardTask?.cancel()
        discardTask = nil
        discarded = nil
        run(makeJob(item.recording, target: item.target))
    }

    private func keepForUndo(_ item: DiscardedRecording) {
        dropDiscarded()
        discarded = item
        discardTask = Task { [weak self] in
            try? await Task.sleep(for: Self.undoWindow)
            guard let self, !Task.isCancelled else { return }
            self.dropDiscarded()
        }
    }

    /// The Undo is over (its time ran out, or a new recording started): the audio is deleted.
    private func dropDiscarded() {
        discardTask?.cancel()
        discardTask = nil
        guard let item = discarded else { return }
        discarded = nil
        try? FileManager.default.removeItem(at: item.recording.url)
    }

    func cancelProcessing() {
        processingTask?.cancel()
        processingTask = nil
        phase = .idle
        let activity = self.activity
        Task { await activity.cancel() }
        endBackgroundWork()
    }

    func retryLast() {
        guard let job = lastFailedJob, !phase.isBusy, phase != .recording else { return }
        // Re-read the computer and settings (the user may have fixed something), keep the audio.
        run(makeJob(job.recording, target: job.target))
    }

    func dismissResult() {
        if !phase.isBusy, phase != .recording { phase = .idle }
    }

    private func makeJob(_ recording: Recording, target: DictationTarget? = nil, stream: StreamingUploader? = nil) -> DictationJob {
        let link = computerLink()
        return DictationJob(
            recording: recording,
            target: target ?? recordingTarget ?? effectiveTarget,
            computer: link,
            cachedInfo: freshInfo(for: link),
            copyInPCMode: copyInPCMode,
            offlineEngine: offlineEngine,
            vocab: vocab,
            stream: stream
        )
    }

    @discardableResult
    private func run(_ job: DictationJob) -> Task<Void, Never> {
        // Both targets ask a paired PC first (Phone: the PC writes the text when it's at home);
        // a streamed one is mostly there already.
        let first: DictationStage
        if job.stream != nil {
            first = .sendingAudio(.lan)
        } else {
            first = job.computer?.lanEndpoint != nil ? .lookingForComputer : .transcribingOnPhone
        }
        phase = .processing(first.message)
        activity.sending()
        processingTask?.cancel()
        beginBackgroundWork()
        let task = Task { [weak self] in
            guard let self else { return }
            let ending = await self.process(job)
            await ending?.value
            self.endBackgroundWork()
        }
        processingTask = task
        return task
    }

    private func startMetering() {
        meterTask?.cancel()
        meterTask = Task { [weak self] in
            var lastPeak = ContinuousClock.now - .seconds(5)
            while !Task.isCancelled {
                guard let self else { return }
                let raw = self.recorder.level()
                self.level = self.level * 0.35 + raw * 0.65
                self.elapsed = self.recorder.currentTime
                self.activity.level(self.level)
                if raw >= Theme.Motion.loudPeak, ContinuousClock.now - lastPeak > .milliseconds(450) {
                    self.peakTick += 1
                    lastPeak = ContinuousClock.now
                }
                if self.elapsed >= AudioRecorder.maxDuration {
                    self.stopAndSend()
                    return
                }
                try? await Task.sleep(for: Theme.Motion.meterInterval)
            }
        }
    }

    private func stopMetering() {
        meterTask?.cancel()
        meterTask = nil
        level = 0
    }

    /// Keeps the app running after the microphone stops (uploads from the Action Button
    /// happen in the background).
    private func beginBackgroundWork() {
        guard backgroundTaskID == .invalid else { return }
        backgroundTaskID = UIApplication.shared.beginBackgroundTask(withName: "ShoutAbout dictation") { [weak self] in
            MainActor.assumeIsolated {
                self?.endBackgroundWork()
            }
        }
    }

    private func endBackgroundWork() {
        guard backgroundTaskID != .invalid else { return }
        UIApplication.shared.endBackgroundTask(backgroundTaskID)
        backgroundTaskID = .invalid
    }

    // MARK: Routing and delivery

    /// Runs the router; returns the Live Activity's ending task. A streamed dictation asks the
    /// PC for its text first; if the stream didn't work out, the file goes the usual way.
    private func process(_ job: DictationJob) async -> Task<Void, Never>? {
        let started = ContinuousClock.now
        if let stream = job.stream {
            let outcome = await stream.finish(target: job.target, recordedSeconds: job.recording.duration)
            if Task.isCancelled { return nil }
            if let outcome { return finish(outcome, job: job, started: started) }
        }
        let onWiFi = await NetworkPath.current().onWiFi
        let request = DictationRequest(
            audioURL: job.recording.url,
            target: job.target,
            computer: job.computer,
            onWiFi: onWiFi,
            cachedInfo: job.cachedInfo
        )
        let router = DictationRouter(
            transport: LiveHubTransport(),
            onDevice: OnDeviceEngine(vocab: job.vocab, preference: job.offlineEngine)
        )
        let result = await router.run(request) { [weak self] stage in
            await self?.show(stage)
        }
        if Task.isCancelled { return nil }
        switch result {
        case .success(let outcome):
            return finish(outcome, job: job, started: started)
        case .failure(let failure):
            return fail(failure.message, job: job)
        }
    }

    private func show(_ stage: DictationStage) {
        guard phase.isBusy else { return }
        phase = .processing(stage.message)
    }

    private func finish(_ outcome: DictationOutcome, job: DictationJob, started: ContinuousClock.Instant) -> Task<Void, Never>? {
        if let info = outcome.learnedInfo, let key = job.computer?.hubKey {
            storeHubInfo(info, key: key)
        }
        keyboardTypingWatch?.cancel()
        keyboardTypingWatch = nil
        var copyWaits = false
        if !outcome.text.isEmpty, outcome.delivery == .clipboard || job.copyInPCMode {
            copyWaits = !putOnClipboard(outcome.text)
        }
        let item = HistoryItem(
            text: outcome.text,
            rawText: outcome.rawText,
            target: job.target,
            route: outcome.route,
            pasted: outcome.delivery == .pastedOnComputer,
            warning: outcome.notice,
            cleanup: outcome.hubResponse?.cleanup,
            timings: outcome.hubResponse?.timingsMs,
            audioSeconds: job.recording.duration,
            roundTripMs: Self.milliseconds(since: started),
            engine: outcome.engine
        )
        // A phone dictation (any trigger) for the ShoutAbout keyboard, if it's on screen or
        // comes up soon. Never one sent to the computer.
        if !isDemo, KeyboardHandoff.shouldPublish(
            toPhone: job.target == .phone,
            text: outcome.text,
            keyboardSetUp: keyboardLink.keyboardIsSetUp
        ) {
            let handoffID = publishKeyboardHandoff(outcome.text)
            if copyWaits { watchKeyboardTyping(handoffID) }
        }
        complete(item, job: job)
        refreshVocabQuietly()
        let result = DictationActivityState.outcome(pasted: item.pasted, text: item.text)
        let tapToCopy = copyWaits && result == .copied
        let detail = DictationActivityState.doneDetail(result, notice: outcome.notice, computerName: computerName, tapToCopy: tapToCopy)
        return activity.finish(outcome: result, message: detail, tapToCopy: tapToCopy)
    }

    // MARK: Clipboard

    /// Puts a result on the clipboard; false when it has to wait until the app is in front.
    ///
    /// iOS keeps apps in the background away from the clipboard, and a dictation from the
    /// Action Button, Control Center or the Live Activity's Stop finishes with ShoutAbout in
    /// the background (the in-app button finishes in front and copies right away). So it tries
    /// anyway (the pasteboard's change count tells whether it took) and otherwise keeps the
    /// text as a `PendingCopy` (App Group) that `applyPendingCopy` writes the moment the app is
    /// in front. The Live Activity meanwhile says "Ready! · Tap here to copy it": a tap on it
    /// opens the app, which copies.
    private func putOnClipboard(_ text: String) -> Bool {
        if Self.writeClipboard(text) || UIApplication.shared.applicationState == .active {
            discardPendingCopy()
            return true
        }
        if !isDemo { settings.pendingCopy = PendingCopy(text: text, createdAt: Date()) }
        clipboardPending = true
        return false
    }

    /// The app is in front (or shoutabout://copy): a result that waited for it goes on the
    /// clipboard now, if it's recent (`PendingCopy.maxAge`); an older one is left to the
    /// result card's Copy button, so it never replaces something copied since.
    func applyPendingCopy() {
        guard !isDemo, let pending = settings.pendingCopy else { return }
        guard pending.isFresh() else {
            settings.pendingCopy = nil
            return
        }
        // Still coming forward (the URL can arrive before the scene is active): next time.
        guard Self.writeClipboard(pending.text) || UIApplication.shared.applicationState == .active else { return }
        settings.pendingCopy = nil
        clipboardPending = false
        successTick += 1
        activity.copied()
    }

    private func discardPendingCopy() {
        clipboardPending = false
        if !isDemo, settings.pendingCopy != nil { settings.pendingCopy = nil }
    }

    /// A dictation from the system (Action Button, Control Center, Siri, the Live Activity's
    /// Stop) whose text still waits for the clipboard: only the app in front can write it, so
    /// the intent brings the app forward (`DictationLauncher`). Not when the ShoutAbout
    /// keyboard on screen typed it: it put it on the clipboard too.
    func copyWaitsForTheApp() async -> Bool {
        guard clipboardPending else { return false }
        if let watch = keyboardTypingWatch { _ = await watch.value }
        return clipboardPending && UIApplication.shared.applicationState != .active
    }

    /// A phone dictation waiting for the clipboard also went to the keyboard: when the
    /// keyboard on screen types it (within a moment of the hand-off), it has copied it as well.
    private func watchKeyboardTyping(_ id: String) {
        keyboardTypingWatch = Task { [weak self] in
            for _ in 0..<8 {  // the keyboard types it ~0.1-0.3 s after the hand-off
                try? await Task.sleep(for: .milliseconds(100))
                guard let self, !Task.isCancelled else { return false }
                if self.keyboardLink.wasTyped(id) {
                    self.discardPendingCopy()
                    self.activity.copied()
                    return true
                }
            }
            return false
        }
    }

    /// Waits until the dictation being sent has landed (or failed, or was stopped).
    func waitForDelivery() async {
        guard phase.isBusy else { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            deliveryWaiters.append(continuation)
        }
    }

    /// ...and its Live Activity has ended too.
    func waitForCurrentDictation() async {
        await processingTask?.value
    }

    private func resumeDeliveryWaiters() {
        let waiters = deliveryWaiters
        deliveryWaiters = []
        for waiter in waiters {
            waiter.resume()
        }
    }

    /// Writes the general pasteboard; true when it took the text (its change count moved).
    private static func writeClipboard(_ text: String) -> Bool {
        let pasteboard = UIPasteboard.general
        let before = pasteboard.changeCount
        pasteboard.string = text
        return pasteboard.changeCount != before
    }

    private func complete(_ item: HistoryItem, job: DictationJob) {
        record(item)
        if let previous = lastFailedJob, previous.recording.url != job.recording.url {
            try? FileManager.default.removeItem(at: previous.recording.url)
        }
        lastFailedJob = nil
        try? FileManager.default.removeItem(at: job.recording.url)
        phase = .done
        if item.text.isEmpty {
            warningTick += 1
        } else {
            successTick += 1
        }
    }

    private func fail(_ message: String, job: DictationJob) -> Task<Void, Never>? {
        let item = HistoryItem(
            text: "",
            target: job.target,
            route: .offline,
            audioSeconds: job.recording.duration,
            error: message
        )
        record(item)
        if let previous = lastFailedJob, previous.recording.url != job.recording.url {
            try? FileManager.default.removeItem(at: previous.recording.url)
        }
        var kept = job
        kept.stream = nil
        lastFailedJob = kept  // keep the audio for Retry
        showFailure(message)
        return activity.fail(message: message)
    }

    private func showFailure(_ message: String) {
        phase = .failed(message)
        failureTick += 1
    }

    private func record(_ item: HistoryItem) {
        lastItem = item
        history.add(item)  // Recent: in memory only
    }

    private static func milliseconds(since start: ContinuousClock.Instant) -> Int {
        let (seconds, attoseconds) = (ContinuousClock.now - start).components
        return Int(seconds * 1000 + attoseconds / 1_000_000_000_000_000)
    }

    // MARK: Recent

    func copy(_ item: HistoryItem) {
        guard !item.text.isEmpty else { return }
        UIPasteboard.general.string = item.text
        discardPendingCopy()
        successTick += 1
    }

    func deleteHistory(_ ids: Set<UUID>) {
        history.remove(ids)
    }

    // MARK: The computer: pairing

    /// This iPhone's random id for pairing requests.
    var deviceID: String { settings.deviceID() }

    /// The name the PC shows in "<name> wants to dictate to this PC".
    var deviceName: String { UIDevice.current.name }

    /// v2 pairing finished: the PC clicked Allow. A new computer is added (the same PC again
    /// gets the new token) and becomes the selected one.
    func applyDevicePairing(token: String, hub: PairedHubInfo, address: URL, fallbackName: String) throws {
        try keychain.saveDeviceToken(token, for: hub.id)
        let lan = hub.lanURL.flatMap(URLRules.lanBaseURL) ?? address
        let name = hub.name.trimmingCharacters(in: .whitespacesAndNewlines)
        let computer = PairedComputer(id: hub.id, name: name.isEmpty ? fallbackName : name, lanURL: lan, pairedAt: Date())
        let switched = pairedComputer?.id != hub.id
        roster.add(computer, codePairing: hasLegacyToken ? configuration : nil)
        saveRoster()
        computersWithToken.insert(hub.id)
        forgetHubInfo(for: hub.id)
        connectionChecks = []
        pairingChanges += 1
        if switched { vocabFetchedAt = nil }  // the new computer's words, right away
        target = .pc
        Task { await refreshComputer(force: true) }
    }

    /// Settings or the main screen's switch: which computer "Computer" means. Not while a
    /// dictation records or is on its way.
    func selectComputer(_ id: String) {
        guard !computerSwitchLocked, roster.select(id) else { return }
        saveRoster()
        connectionChecks = []
        vocabFetchedAt = nil  // its words, at the next chance
        Task { await refreshComputer(force: true) }
    }

    /// A name for a computer on this iPhone only; empty brings back the PC's own name.
    func renameComputer(_ id: String, to nickname: String) {
        roster.rename(id, to: nickname)
        saveRoster()
    }

    /// Forgets one computer and its token (`ComputerRoster.codePairingID`: the code pairing).
    /// The code pairing and tunnel go too when they were that PC's. When it was the selected
    /// one, the next one is selected; with none left, dictation goes to the phone.
    func removeComputer(_ id: String) {
        guard id != ComputerRoster.codePairingID else {
            forgetLegacyPairing()
            return
        }
        let wasSelected = pairedComputer?.id == id
        try? keychain.saveDeviceToken(nil, for: id)
        if roster.remove(id) {
            keychain.deleteLegacy()
            configuration = HubConfiguration()
            settings.configuration = configuration
        }
        saveRoster()
        forgetHubInfo(for: id)
        refreshSecretsSummary()
        pairingChanges += 1
        if wasSelected {
            // Its word list.
            vocab = []
            settings.vocabTerms = []
            settings.vocabVersion = nil
            settings.vocabFetchedAt = nil
            vocabFetchedAt = nil
            connectionChecks = []
            if hasComputer { Task { await refreshComputer(force: true) } }
        }
        if !hasComputer { target = .phone }
    }

    /// Legacy pairing QR (`python -m shoutabout pair`) or the manual form.
    func applyPairing(_ payload: PairingPayload) throws {
        try keychain.save(payload.secrets)
        configuration = payload.configuration
        settings.configuration = payload.configuration
        roster.assignCodePairing(lanURL: payload.configuration.lanURL)
        saveRoster()
        refreshSecretsSummary()
        connectionChecks = []
        pairingChanges += 1
        if hasComputer { target = .pc }
        Task { await refreshComputer(force: true) }
    }

    /// Advanced: removes only the code (QR) pairing and the tunnel secrets.
    func forgetLegacyPairing() {
        keychain.deleteLegacy()
        configuration = HubConfiguration()
        settings.configuration = configuration
        roster.codePairingOwnerID = nil
        saveRoster()
        connectionChecks = []
        refreshSecretsSummary()
        pairingChanges += 1
        if !hasComputer { target = .phone }
    }

    /// Bare hub token (old `token --qr`): keeps the URLs and Cloudflare token, swaps the token.
    func applyToken(_ token: String) throws {
        var secrets = keychain.secrets() ?? HubSecrets(token: token)
        secrets.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        try keychain.save(secrets)
        refreshSecretsSummary()
        pairingChanges += 1
    }

    func currentSecrets() -> HubSecrets? { keychain.secrets() }

    /// How to reach the selected computer (its own token; the code pairing's token and tunnel
    /// only when they are that PC's). Never another computer's.
    func computerLink() -> ComputerLink? {
        let selected = pairedComputer
        // With no v2 computer the code pairing is the computer; otherwise only its owner's.
        let withCode = selected.map { roster.codePairingOwnerID == $0.id } ?? roster.computers.isEmpty
        return ComputerLink.make(
            paired: selected,
            deviceToken: selected.flatMap { keychain.deviceToken(for: $0.id) },
            legacy: withCode ? configuration : HubConfiguration(),
            legacySecrets: withCode ? keychain.secrets() : nil,
            useTunnel: useTunnel
        )
    }

    private func refreshSecretsSummary() {
        let secrets = keychain.secrets()
        hasLegacyToken = secrets != nil
        hasCloudflareToken = secrets?.hasCloudflareToken ?? false
        computersWithToken = Set(roster.computers.map(\.id).filter { keychain.deviceToken(for: $0) != nil })
    }

    /// Keeps the computers in the App Group and tells Control Center and the widget (they
    /// show the selected computer's name).
    private func saveRoster() {
        guard !isDemo else { return }
        settings.roster = roster
        ControlCenter.shared.reloadControls(ofKind: WidgetKinds.targetControl)
        WidgetCenter.shared.reloadTimelines(ofKind: WidgetKinds.lockScreen)
    }

    /// The PC said its name (`/hello`, `/info`): shown from now on, unless it has a nickname.
    private func noteName(_ name: String?, for id: String) {
        if roster.noteName(name, for: id) { saveRoster() }
    }

    private func freshInfo(for link: ComputerLink?) -> HubInfo? {
        guard let key = link?.hubKey,
              let cached = hubInfos.first(where: { $0.hubKey == key }),
              cached.isFresh(for: key)
        else { return nil }
        return cached.info
    }

    private func storeHubInfo(_ info: HubInfo, key: String) {
        let cached = CachedHubInfo(info: info, fetchedAt: Date(), hubKey: key)
        hubInfos.removeAll { $0.hubKey == key }
        hubInfos.append(cached)
        if !isDemo { settings.cachedHubInfos = hubInfos }
        // Its own name, kept fresh (key is the hub id for a v2 computer).
        if info.id == nil || info.id == key { noteName(info.name, for: key) }
    }

    private func forgetHubInfo(for key: String) {
        hubInfos.removeAll { $0.hubKey == key }
        if !isDemo { settings.cachedHubInfos = hubInfos }
    }

    /// On launch / return to the app: is the selected PC there, how fast is it, did its
    /// address change? And its word list, every time it answers. Then the other computers'
    /// addresses and names. Runs quietly; nothing here blocks a dictation.
    func refreshComputer(force: Bool = false) async {
        guard !isDemo, hasComputer else { return }
        if !force, let last = lastRefresh, Date().timeIntervalSince(last) < 60 { return }
        lastRefresh = Date()
        guard let link = computerLink() else { return }
        let transport = LiveHubTransport()
        await refreshSelectedComputer(link, force: force, transport: transport)
        await refreshOtherComputers(transport)
    }

    private func refreshSelectedComputer(_ link: ComputerLink, force: Bool, transport: LiveHubTransport) async {
        guard let lan = link.lanEndpoint else {
            await refreshVocabOverTunnel(link)
            return
        }
        var base: URL?
        if let hello = try? await transport.hello(lan.baseURL, timeout: 2), hello.isShoutAbout,
           link.hubID == nil || hello.id == nil || hello.id == link.hubID {
            base = lan.baseURL
            if let id = link.hubID, hello.id == id { noteName(hello.name, for: id) }
        } else if let id = link.hubID, let paired = roster.computer(id) {
            base = await newAddress(of: paired, transport: transport)
        }
        guard let base else {
            await refreshVocabOverTunnel(link)
            return
        }

        var endpoint = lan
        endpoint.baseURL = base
        if force || freshInfo(for: link) == nil, let key = link.hubKey,
           let info = try? await transport.info(endpoint, timeout: 3) {
            storeHubInfo(info, key: key)
        }
        // A cheap GET every time: words added on the PC reach this iPhone right away (unless
        // another computer was selected meanwhile).
        if let list = try? await HubClient.fetchVocab(endpoint), selectedHubKey == link.hubKey {
            storeVocab(list)
        }
    }

    /// The other computers: still at their address (else found again with Bonjour), and their
    /// current names. Only the unauthenticated `/hello`; nothing else goes to them.
    private func refreshOtherComputers(_ transport: LiveHubTransport) async {
        let selectedID = pairedComputer?.id
        for computer in roster.computers where computer.id != selectedID {
            if let hello = try? await transport.hello(computer.lanURL, timeout: 2), hello.id == computer.id {
                noteName(hello.name, for: computer.id)
            } else {
                _ = await newAddress(of: computer, transport: transport)
            }
        }
    }

    /// The same PC at a new address (a new DHCP lease): found with Bonjour by its id, confirmed
    /// with `/hello`, remembered.
    private func newAddress(of computer: PairedComputer, transport: LiveHubTransport) async -> URL? {
        guard let found = await finder.findAddress(of: computer.id),
              found != computer.lanURL,
              let hello = try? await transport.hello(found, timeout: 2),
              hello.id == computer.id
        else { return nil }
        let moved = roster.noteAddress(found, for: computer.id)
        let renamed = roster.noteName(hello.name, for: computer.id)
        if moved || renamed { saveRoster() }
        return found
    }

    /// The PC isn't on this network: over the owner's tunnel (if set up), at most hourly.
    private func refreshVocabOverTunnel(_ link: ComputerLink) async {
        guard let tunnel = link.tunnel,
              vocabFetchedAt.map({ Date().timeIntervalSince($0) > 3600 }) ?? true,
              let list = try? await HubClient.fetchVocab(tunnel),
              selectedHubKey == link.hubKey
        else { return }
        storeVocab(list)
    }

    // MARK: Connection check (Settings)

    func testConnection() async {
        guard !isCheckingConnection else { return }
        isCheckingConnection = true
        defer { isCheckingConnection = false }
        guard let link = computerLink() else {
            connectionChecks = [ConnectionCheck(title: "No computer", ok: false, detail: "Connect a computer first.")]
            return
        }
        let transport = LiveHubTransport()
        var checks: [ConnectionCheck] = []

        if let lan = link.lanEndpoint {
            let started = ContinuousClock.now
            let router = DictationRouter(transport: transport, onDevice: OnDeviceEngine(vocab: [], preference: offlineEngine))
            let reachable = await router.isReachable(lan.baseURL, expectedID: link.hubID)
            let ms = HubClient.elapsedMs(since: started)
            if reachable {
                checks.append(ConnectionCheck(title: "Home Wi-Fi", ok: true, detail: "\(computerDisplayName) answered in \(ms) ms."))
                let access = await accessCheck(lan, link: link, transport: transport)
                checks.append(access)
            } else {
                checks.append(ConnectionCheck(
                    title: "Home Wi-Fi",
                    ok: false,
                    detail: "\(computerDisplayName) didn't answer. Is ShoutAbout open on the same Wi-Fi?"
                ))
            }
        }
        if let tunnel = link.tunnel {
            let health = await HubClient.health(tunnel, timeout: HubClient.tunnelProbeTimeout)
            checks.append(ConnectionCheck(
                title: "Away (tunnel)",
                ok: health.ok,
                detail: health.ok ? "Answered in \(health.latencyMs) ms." : health.message
            ))
        }
        connectionChecks = checks
    }

    /// Is this iPhone's token accepted? `/info` on v2 PCs, `/vocab` on older ones.
    private func accessCheck(_ endpoint: HubEndpoint, link: ComputerLink, transport: LiveHubTransport) async -> ConnectionCheck {
        let title = "This iPhone is allowed"
        do {
            let info = try await transport.info(endpoint, timeout: 4)
            if let key = link.hubKey { storeHubInfo(info, key: key) }
            return ConnectionCheck(title: title, ok: true, detail: Self.speedDetail(info))
        } catch {
            let failure = HubFailure.from(error)
            guard failure.status == 404 else {
                return ConnectionCheck(title: title, ok: false, detail: failure.userMessage)
            }
            do {
                let list = try await HubClient.fetchVocab(endpoint)
                storeVocab(list)
                return ConnectionCheck(title: title, ok: true, detail: "Yes. Update ShoutAbout on the PC.")
            } catch {
                return ConnectionCheck(title: title, ok: false, detail: HubFailure.from(error).userMessage)
            }
        }
    }

    static func speedDetail(_ info: HubInfo) -> String {
        switch info.speed {
        case .fast:
            return "Yes, fast."
        case .ok:
            return "Yes."
        case .slow:
            return info.acceptsText
                ? "Yes. This iPhone writes, your PC pastes."
                : "Yes."
        }
    }

    // MARK: Vocabulary (GET/POST /vocab on the hub)

    /// The first route that answers right now (LAN, then the tunnel).
    private func reachableEndpoint() async -> HubEndpoint? {
        guard let link = computerLink() else { return nil }
        if let lan = link.lanEndpoint {
            let router = DictationRouter(transport: LiveHubTransport(), onDevice: OnDeviceEngine(vocab: [], preference: offlineEngine))
            if await router.isReachable(lan.baseURL, expectedID: link.hubID) { return lan }
        }
        if let tunnel = link.tunnel, await HubClient.health(tunnel, timeout: HubClient.tunnelProbeTimeout).ok {
            return tunnel
        }
        return nil
    }

    func refreshVocab(silently: Bool = false) async {
        guard !isDemo else { return }
        guard let endpoint = await reachableEndpoint() else {
            if !silently { vocabMessage = "Couldn't reach your PC." }
            return
        }
        do {
            let list = try await HubClient.fetchVocab(endpoint)
            storeVocab(list)
            if !silently { vocabMessage = "\(list.terms.count) words from your PC." }
        } catch {
            guard !silently else { return }
            let failure = HubFailure.from(error)
            vocabMessage = failure.status == 404 ? "Update ShoutAbout on your PC." : failure.userMessage
        }
    }

    func addVocab(_ rawTerm: String) async {
        let term = rawTerm.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !term.isEmpty else { return }
        guard let endpoint = await reachableEndpoint() else {
            vocabMessage = "Couldn't reach your PC, so \"\(term)\" wasn't added."
            return
        }
        do {
            let result = try await HubClient.addVocab(term, endpoint: endpoint)
            // Read the list back right away: the PC's order (new words go to the top), and
            // the next dictation on this iPhone uses the new word.
            if let list = try? await HubClient.fetchVocab(endpoint) {
                storeVocab(list)
            } else if result.added, !vocab.contains(result.term) {
                vocab.append(result.term)
                settings.vocabTerms = vocab
            }
            vocabMessage = result.added ? "Added \"\(result.term)\"." : "\"\(result.term)\" was already there."
            successTick += 1
        } catch {
            let failure = HubFailure.from(error)
            vocabMessage = failure.status == 404 ? "Update ShoutAbout on your PC." : failure.userMessage
        }
    }

    private func storeVocab(_ list: VocabList) {
        vocab = list.terms
        vocabFetchedAt = Date()
        guard !isDemo else { return }
        settings.vocabTerms = list.terms
        settings.vocabVersion = list.version
        settings.vocabFetchedAt = vocabFetchedAt
    }

    /// Before and after a dictation: the PC's word list, when the PC answers on this network
    /// right now (a quick /hello, then a cheap GET), so a word added on the PC a moment ago is
    /// used. Skipped on cellular, while one is running, and when the list is seconds old; the
    /// saved list stays for dictating away from home.
    private func refreshVocabQuietly() {
        guard !isDemo, hasComputer, vocabRefreshTask == nil else { return }
        if let fetched = vocabFetchedAt, Date().timeIntervalSince(fetched) < 15 { return }
        vocabRefreshTask = Task { [weak self] in
            await self?.fetchVocabOnLAN()
            self?.vocabRefreshTask = nil
        }
    }

    private func fetchVocabOnLAN() async {
        guard let link = computerLink(), let lan = link.lanEndpoint else { return }
        let onWiFi = await NetworkPath.current().onWiFi
        guard onWiFi || URLRules.isVPNHost(lan.baseURL.host() ?? "") else { return }
        let router = DictationRouter(transport: LiveHubTransport(), onDevice: OnDeviceEngine(vocab: [], preference: offlineEngine))
        guard await router.isReachable(lan.baseURL, expectedID: link.hubID),
              let list = try? await HubClient.fetchVocab(lan),
              selectedHubKey == link.hubKey  // still the selected computer
        else { return }
        storeVocab(list)
    }

    // MARK: On-device model (Settings › Advanced)

    func refreshOfflineStatus() async {
        guard !isDemo else { return }
        offlineModelStatus = await OfflineTranscriber.status(preference: offlineEngine, hasVocabulary: !vocab.isEmpty)
    }

    func prepareOffline() async {
        guard !isPreparingOffline else { return }
        isPreparingOffline = true
        defer { isPreparingOffline = false }
        offlineModelStatus = "Downloading…"
        do {
            offlineModelStatus = try await OfflineTranscriber.prepare(preference: offlineEngine)
        } catch {
            offlineModelStatus = error.localizedDescription
        }
    }

    // MARK: Icon

    func syncIconState() {
        loudIcon = UIApplication.shared.alternateIconName != nil
    }

    func setLoudIcon(_ on: Bool) async {
        guard UIApplication.shared.supportsAlternateIcons else { return }
        do {
            try await UIApplication.shared.setAlternateIconName(on ? "AppIcon-Alt" : nil)
            loudIcon = on
        } catch {
            loudIcon = UIApplication.shared.alternateIconName != nil
        }
    }

    // MARK: The ShoutAbout keyboard (Shared/Core/KeyboardBridge.swift)
    //
    // The keyboard can't use the microphone, so this app records for it. A keyboard session
    // keeps the microphone input running in the background (`AudioRecorder.startLiveInput`)
    // for up to 5 minutes after the last dictation, so a tap on the keyboard's mouth (a Darwin
    // `start`) begins one at once without leaving the app you're typing in. Without a session
    // the keyboard opens shoutabout://keyboard, which opens one and starts listening. The state
    // (and a heartbeat) goes to the App Group for the keyboard; phone dictations go there too
    // (`KeyboardHandoff`) and the keyboard types them.

    private func setUpKeyboardLink() {
        keyboardOn = keyboardLink.keyboardIsSetUp
        _ = keyboardLink.handoff()  // removes one past its few minutes
        KeyboardSignals.observe([.start, .stop]) { [weak self] signal in
            self?.handleKeyboardSignal(signal)
        }
        publishKeyboardState()
    }

    private func handleKeyboardSignal(_ signal: KeyboardSignal) {
        switch signal {
        case .start:
            Task { await startForKeyboard(showScreen: false) }
        case .stop:
            stopForKeyboard()
        case .state, .result:
            break  // the app's own signals
        }
    }

    /// shoutabout://keyboard: the keyboard had no live session. Open one and listen; the screen
    /// says to go back (it goes on listening in the background).
    private func openedFromKeyboard() {
        guard sceneActive else {
            keyboardOpenPending = true  // as soon as the app is in front
            return
        }
        Task { await startForKeyboard(showScreen: true) }
    }

    /// Starts a dictation for the keyboard, opening a keyboard session first if there's none
    /// (that needs the app in front: iOS doesn't let a background app open the microphone).
    func startForKeyboard(showScreen: Bool) async {
        guard !isDemo else { return }
        if phase == .recording {
            // Already listening (the Action Button, the app's button): the keyboard follows it.
            if showScreen { keyboardListeningScreen = true }
            publishKeyboardState()
            return
        }
        guard !phase.isBusy, !isStarting, !keyboardStarting else {
            publishKeyboardState()
            return
        }
        keyboardStarting = true
        defer {
            keyboardStarting = false
            keyboardStopRequested = false
        }
        if !recorder.isLiveInputRunning {
            guard UIApplication.shared.applicationState == .active else {
                publishKeyboardState()  // the keyboard opens the app instead
                return
            }
            switch AudioRecorder.permission {
            case .granted:
                break
            case .undetermined:
                guard await AudioRecorder.requestPermission() else {
                    _ = microphoneDenied()
                    return
                }
            case .denied:
                _ = microphoneDenied()
                return
            }
            do {
                try openKeyboardSession()
            } catch {
                showFailure(error.localizedDescription)
                return
            }
        }
        guard await startRecording(forKeyboard: true) == .started else { return }
        if showScreen { keyboardListeningScreen = true }
        if keyboardStopRequested { stopAndSend() }  // the keyboard said stop while it started
    }

    private func stopForKeyboard() {
        if phase == .recording {
            stopAndSend()
        } else if keyboardStarting {
            keyboardStopRequested = true
        } else {
            publishKeyboardState()  // it thought this app was listening: tell it how things are
        }
    }

    private func openKeyboardSession() throws {
        try recorder.startLiveInput { [weak self] in
            self?.keyboardInputLost()
        }
        keyboardSessionLastUse = Date()
        publishKeyboardState()
        ensureKeyboardTicker()
    }

    /// Ends the keyboard session (the microphone indicator goes off). A dictation still being
    /// recorded is sent first.
    func closeKeyboardSession() {
        guard recorder.isLiveInputRunning else { return }
        if phase == .recording { stopAndSend() }
        recorder.stopLiveInput()
        publishKeyboardState()
    }

    /// iOS took the microphone away (a call, another app recording, a route change).
    private func keyboardInputLost() {
        closeKeyboardSession()
    }

    private func keyboardPhaseChanged(from old: DictationPhase) {
        guard !isDemo else { return }
        if phase != .recording, keyboardListeningScreen { keyboardListeningScreen = false }
        if old == .recording || phase == .recording { keyboardSessionLastUse = Date() }
        guard old.keyboardPhase != phase.keyboardPhase else { return }
        publishKeyboardState()
        if phase == .recording || phase.isBusy { ensureKeyboardTicker() }
    }

    /// Tells the keyboard (App Group + a Darwin `state`), with the heartbeat.
    private func publishKeyboardState() {
        guard !isDemo else { return }
        let now = Date()
        let current = phase.keyboardPhase
        if current != keyboardPublishedPhase {
            keyboardPublishedPhase = current
            keyboardPhaseSince = now
        }
        keyboardLink.appState = .heartbeat(
            phase: current,
            since: keyboardPhaseSince,
            sessionOpen: recorder.isLiveInputRunning,
            now: now
        )
        KeyboardSignals.post(.state)
    }

    /// While a session is open or a dictation runs: the heartbeat every few seconds, and the
    /// session's idle timeout.
    private func ensureKeyboardTicker() {
        guard keyboardTicker == nil, !isDemo else { return }
        keyboardTicker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(KeyboardAppState.refreshInterval))
                guard let self, !Task.isCancelled else { return }
                guard self.keyboardTick() else {
                    self.keyboardTicker = nil
                    return
                }
            }
        }
    }

    /// One heartbeat; false when there's nothing left to keep alive.
    private func keyboardTick() -> Bool {
        let dictating = phase == .recording || phase.isBusy || keyboardStarting
        if recorder.isLiveInputRunning,
           KeyboardSessionPolicy.shouldClose(lastUse: keyboardSessionLastUse, now: Date(), dictating: dictating) {
            closeKeyboardSession()
        } else {
            publishKeyboardState()
        }
        return recorder.isLiveInputRunning || dictating
    }

    /// The latest phone dictation, for the keyboard to type. Only ever the one, and gone
    /// after a few minutes, typed or not (separate from Recent). Returns its id.
    @discardableResult
    private func publishKeyboardHandoff(_ text: String) -> String {
        let handoff = KeyboardHandoff(text: text)
        keyboardLink.publish(handoff)
        KeyboardSignals.post(.result)
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(KeyboardHandoff.maxAge + 1))
            _ = self?.keyboardLink.handoff()  // removes it once expired
        }
        return handoff.id
    }
}

extension DictationPhase {
    /// What the keyboard shows for it.
    var keyboardPhase: KeyboardAppState.Phase {
        switch self {
        case .recording: return .listening
        case .processing: return .working
        case .failed: return .failed
        case .idle, .done: return .idle
        }
    }
}

#if DEBUG
// MARK: - Screenshot scenes (Debug builds only)

/// Sample data for simulator screenshots (the iOS workflow's `screenshots` run, driven by the
/// ShoutAboutUITests target): `-ScreenshotScene <name>` fills the app with made-up example
/// values instead of the keychain and the hub, and `-ScreenshotAppearance dark|light` forces
/// the look. Nothing is saved, nothing touches the network, and Release builds don't contain it.
enum ScreenshotScene: String, CaseIterable, Sendable {
    case onboarding
    case discovery
    case pairingCode = "pairing-code"
    case actionButton = "action-button"
    case mainComputer = "main-computer"
    case mainComputers = "main-computers"
    case mainPhone = "main-phone"
    case recording
    case processing
    case result
    case away
    case failed
    case history
    case settings
    case islandListening = "island-listening"
    case islandSending = "island-sending"
    case islandDone = "island-done"
    case islandAlert = "island-alert"

    static var current: ScreenshotScene? {
        UserDefaults.standard.string(forKey: "ScreenshotScene").flatMap(ScreenshotScene.init(rawValue:))
    }

    static var forcedColorScheme: ColorScheme? {
        switch UserDefaults.standard.string(forKey: "ScreenshotAppearance") {
        case "dark": return .dark
        case "light": return .light
        default: return nil
        }
    }

    /// The first-launch screen this scene shows (nil: the main screen).
    var onboardingStep: OnboardingStep? {
        switch self {
        case .onboarding: return .choose
        case .discovery, .pairingCode: return .computer
        case .actionButton: return .actionButton
        default: return nil
        }
    }

    /// The sheet open on top of the main screen.
    var sheet: MainSheet? {
        switch self {
        case .history: return .history
        case .settings: return .settings
        default: return nil
        }
    }

    static let sampleComputer = DiscoveredComputer(
        id: "00000000-sample-desktop", name: "Sample-Desktop", version: "2.0.0", serviceName: "Sample-Desktop"
    )
    static let sampleComputers = [
        sampleComputer,
        DiscoveredComputer(id: "00000000-sample-laptop", name: "Sample-Laptop", version: "2.0.0", serviceName: "Sample-Laptop"),
    ]
}

extension AppModel {
    fileprivate func applyScreenshotScene(_ scene: ScreenshotScene) {
        vocab = ["ShoutAbout", "TestFlight", "Sample Project", "faster-whisper"]
        vocabFetchedAt = Date()
        history = HistoryStore(Self.sampleHistory)
        // A made-up PC at a documentation-only address (192.0.2.0/24, RFC 5737).
        let sample = ScreenshotScene.sampleComputer
        let sampleURL = URL(string: "http://192.0.2.10:8766") ?? URL(fileURLWithPath: "/")
        roster = ComputerRoster(
            computers: [PairedComputer(id: sample.id, name: sample.name, lanURL: sampleURL, pairedAt: Date())],
            selectedID: sample.id
        )
        computersWithToken = [sample.id]
        hubInfos = [CachedHubInfo(
            info: HubInfo(
                id: sample.id, name: sample.name, version: "2.0.0",
                stt: HubInfo.STT(tier: "best", device: "cuda", speed: "fast"),
                cleanup: true, accepts: ["audio", "text"]
            ),
            fetchedAt: Date(),
            hubKey: sample.id
        )]
        onboardingDone = true
        target = .pc
        offlineModelStatus = "Model installed"
        keyboardOn = true
        if scene == .mainComputers || scene == .settings {
            // Two computers with the names Windows makes up: they show as Desktop and Laptop.
            let laptop = ScreenshotScene.sampleComputers[1]
            roster = ComputerRoster(
                computers: [
                    PairedComputer(id: sample.id, name: "DESKTOP-SAMPLE1", lanURL: sampleURL, pairedAt: Date()),
                    PairedComputer(
                        id: laptop.id, name: "LAPTOP-SAMPLE2",
                        lanURL: URL(string: "http://192.0.2.11:8766") ?? sampleURL, pairedAt: Date()
                    ),
                ],
                selectedID: sample.id
            )
            computersWithToken = [sample.id, laptop.id]
        }

        switch scene {
        case .onboarding, .discovery, .pairingCode, .actionButton:
            onboardingDone = false
            roster = ComputerRoster()
            computersWithToken = []
            hubInfos = []
            history = HistoryStore()
            target = .phone
        case .mainComputer, .mainComputers:
            break
        case .mainPhone:
            target = .phone
        case .recording, .islandListening, .islandAlert:
            phase = .recording
            recordingTarget = .pc
            level = 0.72
            elapsed = 7
        case .processing, .islandSending:
            phase = .processing(DictationStage.sendingAudio(.lan).message)
        case .result, .islandDone, .history, .settings:
            lastItem = history.items.first
            phase = .done
        case .away:
            let item = HistoryItem(
                text: "Sample text: book the dentist for Tuesday morning and move the gym to Thursday.",
                target: .pc, route: .offline, warning: DictationNotice.away,
                audioSeconds: 4.8, roundTripMs: 1450, engine: "SpeechTranscriber"
            )
            history.add(item)
            lastItem = item
            phase = .done
        case .failed:
            let message = "Your PC isn't reachable, and this iPhone couldn't write it down: the on-device model isn't downloaded yet."
            let item = HistoryItem(text: "", target: .pc, route: .offline, audioSeconds: 5.2, error: message)
            lastItem = item
            lastFailedJob = DictationJob(
                recording: Recording(url: FileManager.default.temporaryDirectory.appendingPathComponent("sample.m4a"), duration: 5.2),
                target: .pc, computer: nil, cachedInfo: nil, copyInPCMode: false, offlineEngine: .automatic, vocab: []
            )
            phase = .failed(message)
        }
        if scene == .settings {
            connectionChecks = [
                ConnectionCheck(title: "Home Wi-Fi", ok: true, detail: "\(computerDisplayName) answered in 38 ms."),
                ConnectionCheck(title: "This iPhone is allowed", ok: true, detail: Self.speedDetail(hubInfo?.info ?? HubInfo())),
            ]
        }
    }

    /// Island scenes: put a sample Live Activity up once the app is in the foreground (the UI
    /// test then presses Home, long-presses the island or locks the phone).
    fileprivate func runScreenshotSceneEffects() async {
        guard !demoEffectsDone, let scene = ScreenshotScene.current else { return }
        demoEffectsDone = true
        let state = DictationActivityState(
            phase: .listening, target: .pc, computerName: "Sample-Desktop",
            startedAt: Date().addingTimeInterval(-7), level: 3
        )
        switch scene {
        case .islandListening:
            await activity.showDemo(state)
        case .islandSending:
            var sending = state
            sending.phase = .sending
            sending.frame = 3
            await activity.showDemo(sending)
        case .islandDone:
            var done = state
            done.phase = .done
            done.outcome = .pasted
            done.message = DictationActivityState.doneDetail(.pasted, notice: nil, computerName: "Sample-Desktop")
            await activity.showDemo(done)
        case .islandAlert:
            await activity.showDemo(state)
            beginBackgroundWork()
            try? await Task.sleep(for: .seconds(6))
            var done = state
            done.phase = .done
            done.outcome = .pasted
            done.message = DictationActivityState.doneDetail(.pasted, notice: nil, computerName: "Sample-Desktop")
            await activity.alertDemo(done)
            try? await Task.sleep(for: .seconds(10))
            endBackgroundWork()
        default:
            break
        }
    }

    private static var sampleHistory: [HistoryItem] {
        let now = Date()
        return [
            HistoryItem(
                date: now.addingTimeInterval(-40),
                text: "Sample text: push the release to Friday and tell the team the build is green.",
                target: .pc, route: .lan, pasted: true, cleanup: "applied",
                timings: .init(decode: 12, stt: 148, cleanup: 91, paste: 35, total: 296),
                audioSeconds: 6.4, roundTripMs: 610
            ),
            HistoryItem(
                date: now.addingTimeInterval(-25 * 60),
                text: "Sample text: remember oat milk and coffee filters on the way home.",
                target: .phone, route: .offline,
                audioSeconds: 4.1, roundTripMs: 980, engine: "SpeechTranscriber"
            ),
            HistoryItem(
                date: now.addingTimeInterval(-3 * 3600),
                text: "Sample text: the slides look great, ship them after lunch.",
                target: .pc, route: .lan, pasted: true, cleanup: "applied",
                timings: .init(decode: 10, stt: 139, cleanup: 94, paste: 33, total: 281),
                audioSeconds: 5.0, roundTripMs: 560
            ),
        ]
    }
}
#endif

/// App-only entry points for the shared intents (compiled only with SHOUTABOUT_APP).
@MainActor
enum DictationLauncher {
    enum SystemOutcome: Equatable {
        case started
        case finished
        /// Delivered, but the text waits for the clipboard, which iOS lets only the app in
        /// front write: the intent brings the app forward (`continueInForeground`), then
        /// `copyWaitingText`.
        case copyWaits
        case busy
        case needsForeground(String)
        case failed(String)
    }

    /// DictateIntent (Action Button, Control Center, Siri): start, or stop and send. Waits
    /// for the delivery when stopping, so iOS keeps the app alive while it uploads.
    static func toggleFromSystem() async -> SystemOutcome {
        let model = AppModel.shared
        switch model.phase {
        case .recording:
            return await deliver(model.stopAndSend())
        case .processing:
            return .busy
        default:
            switch await model.startRecording() {
            case .started: return .started
            case .busy: return .busy
            case .needsForeground(let reason): return .needsForeground(reason)
            case .failed(let reason): return .failed(reason)
            }
        }
    }

    /// The Live Activity's Stop button. True when the text waits for the app to copy it (see
    /// `SystemOutcome.copyWaits`).
    static func stopFromSystem() async -> Bool {
        let model = AppModel.shared
        guard model.phase == .recording else { return false }
        return await deliver(model.stopAndSend()) == .copyWaits
    }

    /// Waits for the delivery. When its text couldn't go on the clipboard from the background,
    /// says so right away (the intent then brings the app forward, which copies it);
    /// otherwise waits for the Live Activity to end too, so iOS keeps the app running.
    private static func deliver(_ sending: Task<Void, Never>?) async -> SystemOutcome {
        guard let sending else { return .finished }
        let model = AppModel.shared
        await model.waitForDelivery()
        if await model.copyWaitsForTheApp() { return .copyWaits }
        await sending.value
        return .finished
    }

    /// After `continueInForeground` for a waiting copy (or when iOS didn't allow it): copies
    /// it if the app is in front (otherwise the scene does when it is, or the Live Activity's
    /// "Tap here to copy it"), then waits for the Live Activity to end.
    static func copyWaitingText() async {
        let model = AppModel.shared
        model.applyPendingCopy()
        await model.waitForCurrentDictation()
    }

    /// After `continueInForeground`: record as soon as the scene is active.
    static func startWhenActive() {
        AppModel.shared.startWhenActive()
    }

    static func handleInAppIntent() {
        AppModel.shared.handleDictateCommand()
    }

    static func targetChangedElsewhere() {
        AppModel.shared.reloadSharedState()
    }
}
