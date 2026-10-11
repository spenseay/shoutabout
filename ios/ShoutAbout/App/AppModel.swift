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
}

enum StartResult: Equatable, Sendable {
    case started
    case busy
    /// Only the foreground can do it (ask for the microphone, or Live Activities are off).
    case needsForeground(String)
    case failed(String)
}

/// App state: settings (mirrored to the App Group), the paired computer, the dictation state
/// machine with its Live Activity, history and vocabulary. Views bind to it; intents reach it
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
    /// Settings › Keep history: off by default (see `SharedSettings.resolveKeepHistory`). Off,
    /// results show on the main screen for this session only.
    private(set) var keepHistory = false
    private(set) var onboardingDone = false

    // MARK: The computer

    /// v2 pairing (Bonjour + Allow on the PC).
    private(set) var pairedComputer: PairedComputer?
    /// Legacy QR / manual pairing (LAN URL, public URL, Wi-Fi name).
    private(set) var configuration = HubConfiguration()
    private(set) var hasDeviceToken = false
    private(set) var hasLegacyToken = false
    private(set) var hasCloudflareToken = false
    /// The PC's last `/info` (speed, name).
    private(set) var hubInfo: CachedHubInfo?
    private(set) var vocab: [String] = []
    private(set) var vocabFetchedAt: Date?
    private(set) var loudIcon = false

    // MARK: Dictation state

    private(set) var phase: DictationPhase = .idle
    /// Smoothed mic level 0...1 while recording (drives the mouth).
    private(set) var level: Double = 0
    private(set) var elapsed: TimeInterval = 0
    private(set) var lastItem: HistoryItem?
    private(set) var history: [HistoryItem] = []
    private(set) var lastFailedJob: DictationJob?
    /// Where the current recording goes (fixed when it starts).
    private(set) var recordingTarget: DictationTarget?
    /// The last result isn't on the clipboard: it finished in the background (iOS doesn't let
    /// the app copy there) and waits for the app to be in front, or that took too long and
    /// the Copy button has to do it (see `putOnClipboard`).
    private(set) var clipboardPending = false

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
    @ObservationIgnored private let historyStore = HistoryStore()
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

    private init() {
        target = settings.target
        copyInPCMode = settings.copyInPCMode
        useTunnel = settings.useTunnel
        offlineEngine = settings.offlineEngine
        configuration = settings.configuration
        pairedComputer = settings.pairedComputer
        hubInfo = settings.cachedHubInfo
        vocab = settings.vocabTerms
        vocabFetchedAt = settings.vocabFetchedAt
        history = historyStore.load()
        keepHistory = settings.resolveKeepHistory(existingItemCount: history.count)
        refreshSecretsSummary()
        // People who paired with the QR code before onboarding existed skip it.
        onboardingDone = settings.onboardingDone || (configuration.isPaired && hasLegacyToken)
#if DEBUG
        if let scene = ScreenshotScene.current {
            isDemo = true
            applyScreenshotScene(scene)
        }
#endif
    }

    // MARK: Derived

    /// A computer is paired and has credentials.
    var hasComputer: Bool {
        if pairedComputer != nil, hasDeviceToken || hasLegacyToken { return true }
        return configuration.isPaired && hasLegacyToken
    }

    /// Where a new recording goes: Phone when no computer is paired.
    var effectiveTarget: DictationTarget { hasComputer ? target : .phone }

    /// The PC's name when known ("Sample-Desktop"), for the switch and the Live Activity.
    var computerName: String? {
        pairedComputer?.name ?? hubInfo?.info.name.nonBlank
    }

    /// For sentences: "Sample-Desktop" or "your PC".
    var computerDisplayName: String { computerName ?? "your PC" }

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
        Task { await refreshComputer() }
    }

    /// Re-reads values other processes may have changed (Control Center toggle, intents).
    func reloadSharedState() {
        let stored = settings.target
        if stored != target { target = stored }
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

    /// Starts recording (in-app button, Action Button, Control Center). From the background the
    /// Live Activity must start first: iOS stops a background recording without one.
    func startRecording() async -> StartResult {
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

        let target = effectiveTarget
        let startedAt = Date()
        let live = await activity.start(
            target: target,
            computerName: target == .pc ? computerName : nil,
            startedAt: startedAt
        )
        if !live, UIApplication.shared.applicationState != .active {
            return .needsForeground("Turn on Live Activities in Settings › ShoutAbout.")
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
        recordingTarget = target
        level = 0
        elapsed = 0
        phase = .recording
        startTick += 1
        startMetering()
        discardPendingCopy()  // a new dictation supersedes one still waiting for the clipboard
        refreshVocabQuietly()  // ready by the time you stop talking
        return .started
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
        let activity = self.activity
        guard let recording = recorder.stop() else {
            phase = .idle
            return Task { await activity.cancel() }
        }
        guard recording.duration >= 0.3 else {
            try? FileManager.default.removeItem(at: recording.url)
            phase = .idle
            return Task { await activity.cancel() }
        }
        return run(makeJob(recording))
    }

    func cancelRecording() {
        stopMetering()
        recorder.cancel()
        phase = .idle
        let activity = self.activity
        Task { await activity.cancel() }
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

    private func makeJob(_ recording: Recording, target: DictationTarget? = nil) -> DictationJob {
        let link = computerLink()
        return DictationJob(
            recording: recording,
            target: target ?? recordingTarget ?? effectiveTarget,
            computer: link,
            cachedInfo: freshInfo(for: link),
            copyInPCMode: copyInPCMode,
            offlineEngine: offlineEngine,
            vocab: vocab
        )
    }

    @discardableResult
    private func run(_ job: DictationJob) -> Task<Void, Never> {
        // Both targets ask a paired PC first (Phone: the PC writes the text when it's at home).
        let first: DictationStage = job.computer?.lanEndpoint != nil ? .lookingForComputer : .transcribingOnPhone
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

    /// Runs the router; returns the Live Activity's ending task.
    private func process(_ job: DictationJob) async -> Task<Void, Never>? {
        let started = ContinuousClock.now
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
        lastFailedJob = job  // keep the audio for Retry
        showFailure(message)
        return activity.fail(message: message)
    }

    private func showFailure(_ message: String) {
        phase = .failed(message)
        failureTick += 1
    }

    private func record(_ item: HistoryItem) {
        lastItem = item
        guard keepHistory else { return }  // off: the main screen shows it, nothing is saved
        history.insert(item, at: 0)
        if history.count > HistoryStore.limit { history.removeLast(history.count - HistoryStore.limit) }
        if !isDemo { historyStore.save(history) }
    }

    private static func milliseconds(since start: ContinuousClock.Instant) -> Int {
        let (seconds, attoseconds) = (ContinuousClock.now - start).components
        return Int(seconds * 1000 + attoseconds / 1_000_000_000_000_000)
    }

    // MARK: History

    func copy(_ item: HistoryItem) {
        guard !item.text.isEmpty else { return }
        UIPasteboard.general.string = item.text
        discardPendingCopy()
        successTick += 1
    }

    func deleteHistory(_ ids: Set<UUID>) {
        history.removeAll { ids.contains($0.id) }
        historyStore.save(history)
    }

    func clearHistory() {
        history = []
        lastItem = nil
        historyStore.save(history)
    }

    /// Settings › Keep history. Turning it off can also clear what's saved (the screen asks).
    func setKeepHistory(_ on: Bool, clearSaved: Bool = false) {
        keepHistory = on
        if !isDemo { settings.keepHistory = on }
        if clearSaved { clearHistory() }
    }

    // MARK: The computer: pairing

    /// This iPhone's random id for pairing requests.
    var deviceID: String { settings.deviceID() }

    /// The name the PC shows in "<name> wants to dictate to this PC".
    var deviceName: String { UIDevice.current.name }

    /// v2 pairing finished: the PC clicked Allow.
    func applyDevicePairing(token: String, hub: PairedHubInfo, address: URL, fallbackName: String) throws {
        try keychain.saveDeviceToken(token)
        let lan = hub.lanURL.flatMap(URLRules.lanBaseURL) ?? address
        let name = hub.name.trimmingCharacters(in: .whitespacesAndNewlines)
        let computer = PairedComputer(id: hub.id, name: name.isEmpty ? fallbackName : name, lanURL: lan, pairedAt: Date())
        pairedComputer = computer
        settings.pairedComputer = computer
        hasDeviceToken = true
        hubInfo = nil
        settings.cachedHubInfo = nil
        connectionChecks = []
        target = .pc
        Task { await refreshComputer(force: true) }
    }

    /// Forgets the computer: the device token, the legacy pairing and the tunnel secrets.
    func disconnectComputer() {
        try? keychain.saveDeviceToken(nil)
        keychain.deleteLegacy()
        pairedComputer = nil
        settings.pairedComputer = nil
        configuration = HubConfiguration()
        settings.configuration = configuration
        hubInfo = nil
        settings.cachedHubInfo = nil
        vocab = []
        settings.vocabTerms = []
        settings.vocabVersion = nil
        settings.vocabFetchedAt = nil
        vocabFetchedAt = nil
        connectionChecks = []
        refreshSecretsSummary()
        target = .phone
    }

    /// Legacy pairing QR (`python -m shoutabout pair`) or the manual form.
    func applyPairing(_ payload: PairingPayload) throws {
        try keychain.save(payload.secrets)
        configuration = payload.configuration
        settings.configuration = payload.configuration
        refreshSecretsSummary()
        connectionChecks = []
        if hasComputer { target = .pc }
        Task { await refreshComputer(force: true) }
    }

    /// Advanced: removes only the code (QR) pairing and the tunnel secrets.
    func forgetLegacyPairing() {
        keychain.deleteLegacy()
        configuration = HubConfiguration()
        settings.configuration = configuration
        connectionChecks = []
        refreshSecretsSummary()
        if !hasComputer { target = .phone }
    }

    /// Bare hub token (old `token --qr`): keeps the URLs and Cloudflare token, swaps the token.
    func applyToken(_ token: String) throws {
        var secrets = keychain.secrets() ?? HubSecrets(token: token)
        secrets.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        try keychain.save(secrets)
        refreshSecretsSummary()
    }

    func currentSecrets() -> HubSecrets? { keychain.secrets() }

    func computerLink() -> ComputerLink? {
        ComputerLink.make(
            paired: pairedComputer,
            deviceToken: keychain.deviceToken(),
            legacy: configuration,
            legacySecrets: keychain.secrets(),
            useTunnel: useTunnel
        )
    }

    private func refreshSecretsSummary() {
        let secrets = keychain.secrets()
        hasLegacyToken = secrets != nil
        hasCloudflareToken = secrets?.hasCloudflareToken ?? false
        hasDeviceToken = keychain.deviceToken() != nil
    }

    private func freshInfo(for link: ComputerLink?) -> HubInfo? {
        guard let key = link?.hubKey, let cached = hubInfo, cached.isFresh(for: key) else { return nil }
        return cached.info
    }

    private func storeHubInfo(_ info: HubInfo, key: String) {
        let cached = CachedHubInfo(info: info, fetchedAt: Date(), hubKey: key)
        hubInfo = cached
        if !isDemo { settings.cachedHubInfo = cached }
    }

    /// On launch / return to the app: is the PC there, how fast is it, did its address change?
    /// And its word list, every time it answers. Runs quietly; nothing here blocks a dictation.
    func refreshComputer(force: Bool = false) async {
        guard !isDemo, hasComputer else { return }
        if !force, let last = lastRefresh, Date().timeIntervalSince(last) < 60 { return }
        lastRefresh = Date()
        guard let link = computerLink() else { return }
        guard let lan = link.lanEndpoint else {
            await refreshVocabOverTunnel(link)
            return
        }
        let transport = LiveHubTransport()

        var base: URL?
        if let hello = try? await transport.hello(lan.baseURL, timeout: 2), hello.isShoutAbout,
           link.hubID == nil || hello.id == nil || hello.id == link.hubID {
            base = lan.baseURL
        } else if let paired = pairedComputer,
                  let found = await finder.findAddress(of: paired.id),
                  found != paired.lanURL,
                  let hello = try? await transport.hello(found, timeout: 2),
                  hello.id == paired.id {
            // Same PC, new address (a new DHCP lease): remember it.
            var updated = paired
            updated.lanURL = found
            pairedComputer = updated
            settings.pairedComputer = updated
            base = found
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
        // A cheap GET every time: words added on the PC reach this iPhone right away.
        if let list = try? await HubClient.fetchVocab(endpoint) {
            storeVocab(list)
        }
    }

    /// The PC isn't on this network: over the owner's tunnel (if set up), at most hourly.
    private func refreshVocabOverTunnel(_ link: ComputerLink) async {
        guard let tunnel = link.tunnel,
              vocabFetchedAt.map({ Date().timeIntervalSince($0) > 3600 }) ?? true,
              let list = try? await HubClient.fetchVocab(tunnel)
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
              let list = try? await HubClient.fetchVocab(lan)
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
        history = Self.sampleHistory
        keepHistory = true
        // A made-up PC at a documentation-only address (192.0.2.0/24, RFC 5737).
        let sample = ScreenshotScene.sampleComputer
        pairedComputer = PairedComputer(
            id: sample.id,
            name: sample.name,
            lanURL: URL(string: "http://192.0.2.10:8766") ?? URL(fileURLWithPath: "/"),
            pairedAt: Date()
        )
        hasDeviceToken = true
        hubInfo = CachedHubInfo(
            info: HubInfo(
                id: sample.id, name: sample.name, version: "2.0.0",
                stt: HubInfo.STT(tier: "best", device: "cuda", speed: "fast"),
                cleanup: true, accepts: ["audio", "text"]
            ),
            fetchedAt: Date(),
            hubKey: sample.id
        )
        onboardingDone = true
        target = .pc
        offlineModelStatus = "Model installed"

        switch scene {
        case .onboarding, .discovery, .pairingCode, .actionButton:
            onboardingDone = false
            pairedComputer = nil
            hasDeviceToken = false
            hubInfo = nil
            history = []
            target = .phone
        case .mainComputer:
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
            lastItem = history.first
            phase = .done
        case .away:
            let item = HistoryItem(
                text: "Sample text: book the dentist for Tuesday morning and move the gym to Thursday.",
                target: .pc, route: .offline, warning: DictationNotice.away,
                audioSeconds: 4.8, roundTripMs: 1450, engine: "SpeechTranscriber"
            )
            history.insert(item, at: 0)
            lastItem = item
            phase = .done
        case .failed:
            let message = "Your PC isn't reachable, and this iPhone couldn't write it down: the on-device model isn't downloaded yet."
            let item = HistoryItem(text: "", target: .pc, route: .offline, audioSeconds: 5.2, error: message)
            history.insert(item, at: 0)
            lastItem = item
            lastFailedJob = DictationJob(
                recording: Recording(url: FileManager.default.temporaryDirectory.appendingPathComponent("sample.m4a"), duration: 5.2),
                target: .pc, computer: nil, cachedInfo: nil, copyInPCMode: false, offlineEngine: .automatic, vocab: []
            )
            phase = .failed(message)
        }
        if scene == .settings {
            connectionChecks = [
                ConnectionCheck(title: "Home Wi-Fi", ok: true, detail: "Sample-Desktop answered in 38 ms."),
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
            HistoryItem(
                date: now.addingTimeInterval(-26 * 3600),
                text: "Sample text: ideas from the walk, a brighter icon and a louder mouth.",
                target: .pc, route: .offline, warning: DictationNotice.away,
                audioSeconds: 7.8, roundTripMs: 1900, engine: "SpeechTranscriber"
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
            await model.stopAndSend()?.value
            return .finished
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

    /// The Live Activity's Stop button.
    static func stopFromSystem() async {
        let model = AppModel.shared
        guard model.phase == .recording else { return }
        await model.stopAndSend()?.value
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
