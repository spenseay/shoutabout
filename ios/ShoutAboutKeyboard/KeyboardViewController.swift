import SwiftUI
import UIKit

/// The ShoutAbout keyboard: a big mouth (tap to talk, tap again to stop), the globe when iOS
/// wants one, space, delete and return. A keyboard can't use the microphone, so the app records
/// (Shared/Core/KeyboardBridge.swift) and the words land at the cursor by themselves.
///
/// Privacy: nothing here goes over the network, and nothing typed is kept or logged. It reads
/// the app's state and the latest phone dictation from the App Group (which needs Full Access),
/// and reads the text just before the cursor only to decide on a space.
final class KeyboardViewController: UIInputViewController {
    static let height: CGFloat = 236

    private let state = KeyboardState()
    private let store = KeyboardLinkStore()
    /// This keyboard instance, for the once-only claim on a dictation.
    private let claimant = UUID().uuidString
    private var appState: KeyboardAppState?
    /// A `start` went to the app, which hasn't said "listening" yet.
    private var startPendingSince: Date?
    private var visible = false
    private var claimingID: String?
    private var ticker: Task<Void, Never>?
    private var deleteRepeat: Task<Void, Never>?
    private var zipEnd: Task<Void, Never>?

    override func viewDidLoad() {
        super.viewDidLoad()
        state.hasFullAccess = hasFullAccess
        let hosting = UIHostingController(rootView: KeyboardView(state: state, actions: makeActions()))
        hosting.safeAreaRegions = []
        hosting.view.backgroundColor = .clear
        hosting.view.translatesAutoresizingMaskIntoConstraints = false
        addChild(hosting)
        view.addSubview(hosting.view)
        NSLayoutConstraint.activate([
            hosting.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            hosting.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            hosting.view.topAnchor.constraint(equalTo: view.topAnchor),
            hosting.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        hosting.didMove(toParent: self)

        let height = view.heightAnchor.constraint(equalToConstant: Self.height)
        height.priority = UILayoutPriority(999)
        height.isActive = true
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        visible = true
        let fullAccess = hasFullAccess
        if state.hasFullAccess != fullAccess { state.hasFullAccess = fullAccess }
        if fullAccess { store.fullAccessSeenAt = Date() }  // Settings › Keyboard: On
        KeyboardSignals.observe([.state, .result]) { [weak self] signal in
            self?.received(signal)
        }
        refresh()
        startTicker()
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        visible = false
        startPendingSince = nil
        ticker?.cancel()
        ticker = nil
        stopDeleting()
    }

    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let globe = needsInputModeSwitchKey
        if state.showsGlobe != globe { state.showsGlobe = globe }
    }

    private func makeActions() -> KeyboardActions {
        KeyboardActions(
            mouth: { [weak self] in self?.mouthTapped() },
            again: { [weak self] in self?.typeAgain() },
            space: { [weak self] in self?.textDocumentProxy.insertText(" ") },
            newline: { [weak self] in self?.textDocumentProxy.insertText("\n") },
            deleteDown: { [weak self] in self?.startDeleting() },
            deleteUp: { [weak self] in self?.stopDeleting() },
            inputModeTarget: { [weak self] in self }
        )
    }

    // MARK: The app's state

    private func received(_ signal: KeyboardSignal) {
        guard visible else { return }
        switch signal {
        case .state, .result:
            refresh()
        case .start, .stop:
            break  // the keyboard's own signals
        }
    }

    /// Every second while on screen too: a Darwin notification can be missed, and a stopped
    /// app's state expires (`KeyboardAppState.aliveUntil`).
    private func startTicker() {
        ticker?.cancel()
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, !Task.isCancelled else { return }
                self.refresh()
            }
        }
    }

    /// Reads the app's state, falls back to opening the app if a start went unanswered, and
    /// types a dictation that's waiting.
    private func refresh() {
        guard hasFullAccess else {
            updateMouth()
            return
        }
        appState = store.appState
        let now = Date()
        if let since = startPendingSince {
            switch appState?.phase(at: now) ?? .idle {
            case .listening, .working:
                startPendingSince = nil  // it answered
            case .idle, .failed:
                if KeyboardStartWait.shouldOpenApp(pendingSince: since, app: appState, now: now) {
                    startPendingSince = nil
                    openApp()
                }
            }
        }
        updateMouth()
        typeWaitingDictation()
    }

    private func updateMouth() {
        let mouth = KeyboardMouthState.make(
            app: hasFullAccess ? appState : nil,
            startPending: startPendingSince != nil,
            now: Date()
        )
        if state.mouth != mouth { state.mouth = mouth }
    }

    // MARK: The mouth

    private func mouthTapped() {
        guard hasFullAccess else { return }
        appState = store.appState
        let now = Date()
        switch KeyboardMouthAction.decide(hasFullAccess: true, app: appState, startPending: startPendingSince != nil, now: now) {
        case .signalStart:
            startPendingSince = now
            KeyboardSignals.post(.start)
        case .openApp:
            openApp()
        case .signalStop:
            startPendingSince = nil
            KeyboardSignals.post(.stop)
        case .ignore:
            break
        }
        updateMouth()
    }

    /// No live session: the app opens one and starts listening (shoutabout://keyboard).
    private func openApp() {
        guard let url = URL(string: "shoutabout://keyboard") else { return }
        if !AppOpener.open(url, from: self) {
            extensionContext?.open(url, completionHandler: nil)
        }
    }

    // MARK: Typing a dictation

    /// The latest phone dictation, if it's fresh and no keyboard typed it yet. Claimed first
    /// (compare-and-set in the App Group), then typed after a short settle if the claim stood,
    /// so two keyboards never both type it.
    private func typeWaitingDictation() {
        guard hasFullAccess, visible, claimingID == nil else { return }
        let now = Date()
        guard let handoff = store.handoff(now: now), handoff.canAutoInsert(at: now) else { return }
        guard store.claim(handoff, by: claimant) else { return }
        claimingID = handoff.id
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(80))
            guard let self else { return }
            self.claimingID = nil
            guard self.store.holdsClaim(handoff, by: self.claimant) else { return }
            guard self.visible else {
                self.store.releaseClaim(handoff, by: self.claimant)  // the next keyboard types it
                return
            }
            self.store.removeHandoff(id: handoff.id)
            self.type(handoff.text)
        }
    }

    private func typeAgain() {
        guard let text = state.lastText else { return }
        type(text)
    }

    private func type(_ text: String) {
        let proxy = textDocumentProxy
        proxy.insertText(KeyboardText.insertion(text, after: proxy.documentContextBeforeInput))
        if state.lastText != text { state.lastText = text }
        // The mouth zips shut.
        state.zipTick += 1
        state.zipping = true
        zipEnd?.cancel()
        zipEnd = Task { [weak self] in
            try? await Task.sleep(for: .seconds(ZipTimeline.duration + 0.1))
            guard let self, !Task.isCancelled else { return }
            self.state.zipping = false
        }
    }

    // MARK: Delete (hold to repeat)

    private func startDeleting() {
        textDocumentProxy.deleteBackward()
        deleteRepeat?.cancel()
        deleteRepeat = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(450))
            while !Task.isCancelled {
                guard let self else { return }
                self.textDocumentProxy.deleteBackward()
                try? await Task.sleep(for: .milliseconds(70))
            }
        }
    }

    private func stopDeleting() {
        deleteRepeat?.cancel()
        deleteRepeat = nil
    }
}
