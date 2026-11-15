import SwiftUI
import UIKit

/// The ShoutAbout keyboard: the iPhone keyboard's layout with the mouth where the emoji key
/// sits. Typing works without Full Access. The mouth needs it: a keyboard can't use the
/// microphone, so the app records (Shared/Core/KeyboardBridge.swift) and the words land at the
/// cursor by themselves. Tap the mouth and talk, then tap anywhere to finish; or hold it, talk
/// and let go.
///
/// Privacy: nothing here goes over the network, and nothing typed is kept or logged. It reads
/// the text just before the cursor to capitalize, correct and suggest (with iOS's own spell
/// checker, on the iPhone), reads the app's state and the latest phone dictation from the App
/// Group (which needs Full Access), and puts a dictation it typed on the clipboard (never reads
/// it).
final class KeyboardViewController: UIInputViewController, KeyTouchSurfaceDelegate {
    /// Holding the mouth this long makes it push-to-talk: letting go finishes.
    private static let holdThreshold: TimeInterval = 0.5
    private static let language = "en_US"

    private let state = KeyboardState()
    private let store = KeyboardLinkStore()
    /// This keyboard instance, for the once-only claim on a dictation.
    private let claimant = UUID().uuidString
    private let surface = KeyTouchSurface()
    private let checker = UITextChecker()
    private var heightConstraint: NSLayoutConstraint?

    // Dictation (the app)
    private var appState: KeyboardAppState?
    /// A `start` went to the app, which hasn't said "listening" yet.
    private var startPendingSince: Date?
    private var visible = false
    private var claimingID: String?
    private var ticker: Task<Void, Never>?
    private var holdStartedAt: Date?
    private var holdTimer: Task<Void, Never>?
    /// After Cancel: the keys come back at once, before the app has caught up.
    private var panelHiddenUntil: Date?
    private var failureShownFor: Date?

    // Typing
    private var autoCapitalization: KeyboardAutoCapitalization = .sentences
    private var corrects = true
    private var lastShiftTap: Date?
    private var lastSpaceAt: Date?
    /// The last automatic correction, so delete right after it puts back what was typed.
    private var lastCorrection: (original: String, replacement: String, suffix: String)?
    /// A suggestion tapped just now added a space: punctuation goes before it.
    private var autoSpace = false
    /// Text replacements from Settings (lower-cased shortcut -> phrase).
    private var replacements: [String: String] = [:]
    /// Names from Contacts: never corrected.
    private var knownWords: Set<String> = []
    private var deleteRepeat: Task<Void, Never>?
    private var noticeEnd: Task<Void, Never>?
    /// The last dictation as typed here (with the space put in front), for Undo and Redo in
    /// the suggestion bar. Only in memory.
    private var lastInsertion: String?

    // The cursor (the space bar as a trackpad)
    /// The text around the cursor while you drag, in lines (`KeyboardTextLines`), and where the
    /// cursor is in it. The text field reports cursor moves back late, so it's read once and
    /// kept in step; read again (after `contextSettle`) once the cursor goes past what it holds.
    private var cursorText: KeyboardTextLines?
    private var cursorPosition = 0
    /// How far along a line up and down aim: kept across a shorter line, like the iPhone's.
    private var cursorGoalX: CGFloat?
    /// Moves that came in while the text was being read again.
    private var cursorQueue = (characters: 0, lines: 0)
    private var cursorReread: Task<Void, Never>?
    private static let contextSettle: Duration = .milliseconds(120)

    override func loadView() {
        // Keyboard clicks (UIDevice.playInputClick) need an input view that asks for them.
        view = KeyboardInputView(frame: .zero, inputViewStyle: .default)
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        state.hasFullAccess = hasFullAccess

        let face = KeyboardFace(state: state, actions: KeyboardFaceActions(
            suggestion: { [weak self] in self?.suggestionTapped($0) },
            history: { [weak self] in self?.historyTapped() }
        ))
        let hosting = UIHostingController(rootView: face)
        hosting.safeAreaRegions = []
        hosting.view.backgroundColor = .clear
        hosting.view.translatesAutoresizingMaskIntoConstraints = false
        addChild(hosting)
        view.addSubview(hosting.view)
        surface.delegate = self
        surface.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(surface)
        for child in [hosting.view!, surface] {
            NSLayoutConstraint.activate([
                child.leadingAnchor.constraint(equalTo: view.leadingAnchor),
                child.trailingAnchor.constraint(equalTo: view.trailingAnchor),
                child.topAnchor.constraint(equalTo: view.topAnchor),
                child.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            ])
        }
        hosting.didMove(toParent: self)

        let height = view.heightAnchor.constraint(equalToConstant: KeyboardLayout.height(compact: false))
        height.priority = UILayoutPriority(999)
        height.isActive = true
        heightConstraint = height

        Task { [weak self] in
            guard let lexicon = await self?.requestSupplementaryLexicon() else { return }
            let entries = lexicon.entries.map { ($0.userInput, $0.documentText) }
            self?.learn(entries)
        }
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
        readTextTraits(appearing: true)
        refreshTyping()
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
        endHold()
        cursorDragEnded()
        state.pressed = []
        state.popup = nil
        dismissHistory()
        lastInsertion = nil
        if hasFullAccess {
            // The app closes its keyboard session (and iOS's microphone dot) soon after,
            // unless a keyboard comes back.
            store.keyboardOnScreenUntil = Date()
            KeyboardSignals.post(.hidden)
        }
    }

    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let globe = needsInputModeSwitchKey
        if state.showsGlobe != globe { state.showsGlobe = globe }
        let compact = traitCollection.verticalSizeClass == .compact
        if state.compact != compact { state.compact = compact }
        let height = KeyboardLayout.height(compact: compact)
        if let heightConstraint, heightConstraint.constant != height { heightConstraint.constant = height }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        layoutKeys()
    }

    override func textDidChange(_ textInput: (any UITextInput)?) {
        super.textDidChange(textInput)
        readTextTraits(appearing: false)
        refreshTyping()
    }

    override func selectionDidChange(_ textInput: (any UITextInput)?) {
        super.selectionDidChange(textInput)
        refreshTyping()
    }

    private func layoutKeys() {
        let size = view.bounds.size
        let bar = state.barHeight
        let frames = KeyboardLayout.frames(
            page: state.page, showsGlobe: state.showsGlobe, width: size.width, top: bar, height: size.height - bar
        )
        if state.keyFrames != frames { state.keyFrames = frames }
        surface.update(frames: frames, keyAreaTop: bar, panelMode: state.showsPanel)
    }

    // MARK: The text field

    /// What the text field asks for: capitals, corrections, the return key, dark or light.
    private func readTextTraits(appearing: Bool) {
        let proxy = textDocumentProxy
        switch proxy.autocapitalizationType {
        case .some(.none): autoCapitalization = .none
        case .some(.words): autoCapitalization = .words
        case .some(.allCharacters): autoCapitalization = .allCharacters
        default: autoCapitalization = .sentences
        }

        let type = proxy.keyboardType ?? .default
        let noCorrections: [UIKeyboardType] = [.URL, .emailAddress, .webSearch, .twitter]
        let plainContent: [UITextContentType] = [.username, .emailAddress, .URL, .password, .newPassword, .oneTimeCode]
        let content: UITextContentType? = proxy.textContentType ?? nil
        corrects = proxy.autocorrectionType != .no
            && !noCorrections.contains(type)
            && !(content.map { plainContent.contains($0) } ?? false)

        let returnKey: (String, Bool)
        switch proxy.returnKeyType ?? .default {
        case .go: returnKey = ("go", true)
        case .google, .search, .yahoo: returnKey = ("search", true)
        case .join: returnKey = ("join", true)
        case .next: returnKey = ("next", false)
        case .route: returnKey = ("route", true)
        case .send: returnKey = ("send", true)
        case .done: returnKey = ("done", true)
        case .emergencyCall: returnKey = ("Emergency", true)
        case .continue: returnKey = ("continue", true)
        default: returnKey = ("return", false)
        }
        if state.returnLabel != returnKey.0 { state.returnLabel = returnKey.0 }
        if state.returnIsPrimary != returnKey.1 { state.returnIsPrimary = returnKey.1 }

        let dark = proxy.keyboardAppearance == .dark
        if state.forceDark != dark { state.forceDark = dark }

        if appearing {
            let numeric: [UIKeyboardType] = [.numbersAndPunctuation, .numberPad, .decimalPad, .phonePad, .asciiCapableNumberPad]
            let page: KeyboardPage = numeric.contains(type) ? .numbers : .letters
            if state.page != page { switchPage(page) }
        }
    }

    /// Capitals and suggestions for where the cursor is now.
    private func refreshTyping() {
        guard !state.showsPanel, !state.trackpad else { return }
        if state.shift != .locked {
            let capital = KeyboardTyping.capitalizes(autoCapitalization, before: textDocumentProxy.documentContextBeforeInput)
            setShift(capital ? .on : .off)
        }
        updateSuggestions()
    }

    private func setShift(_ shift: KeyboardShift) {
        guard state.shift != shift else { return }
        state.shift = shift
        if UIAccessibility.isVoiceOverRunning { surface.refreshAccessibility() }
    }

    private func switchPage(_ page: KeyboardPage) {
        state.page = page
        state.popup = nil
        layoutKeys()
    }

    private func learn(_ entries: [(String, String)]) {
        for (shortcut, phrase) in entries {
            if shortcut.caseInsensitiveCompare(phrase) == .orderedSame {
                knownWords.insert(phrase.lowercased())  // a name from Contacts
            } else {
                replacements[shortcut.lowercased()] = phrase  // a text replacement
            }
        }
    }

    // MARK: Spelling

    private struct Spelling {
        var misspelled: Bool
        var guesses: [String]
        var completions: [String]
        var correction: String?
    }

    private func spelling(of word: String) -> Spelling {
        let range = NSRange(location: 0, length: (word as NSString).length)
        let language = Self.language
        let misspelled = checker.rangeOfMisspelledWord(
            in: word, range: range, startingAt: 0, wrap: false, language: language
        ).location != NSNotFound
        let guesses = misspelled ? (checker.guesses(forWordRange: range, in: word, language: language) ?? []) : []
        let completions = Array((checker.completions(forPartialWordRange: range, in: word, language: language) ?? []).prefix(5))
        let correction = KeyboardTyping.correction(
            for: word,
            misspelled: misspelled,
            guesses: guesses,
            replacement: replacements[word.lowercased()],
            known: knownWords
        )
        return Spelling(misspelled: misspelled, guesses: guesses, completions: completions, correction: correction)
    }

    private func updateSuggestions() {
        let word = KeyboardTyping.currentWord(before: textDocumentProxy.documentContextBeforeInput)
        guard corrects, !word.isEmpty else {
            if !state.suggestions.isEmpty { state.suggestions = [] }
            return
        }
        let found = spelling(of: word)
        let suggestions = KeyboardTyping.suggestions(
            typed: word, correction: found.correction, guesses: found.guesses, completions: found.completions
        )
        if state.suggestions != suggestions { state.suggestions = suggestions }
    }

    /// Corrects the word before the cursor (when that's on), then types `suffix` after it.
    private func correctWord(thenType suffix: String) {
        let proxy = textDocumentProxy
        let word = KeyboardTyping.currentWord(before: proxy.documentContextBeforeInput)
        if corrects, !word.isEmpty, let fix = spelling(of: word).correction {
            for _ in 0..<word.count { proxy.deleteBackward() }
            proxy.insertText(fix + suffix)
            lastCorrection = (word, fix, suffix)
        } else {
            proxy.insertText(suffix)
            lastCorrection = nil
        }
    }

    // MARK: Keys (KeyTouchSurfaceDelegate)

    func keyDown(_ key: KeyboardKey) {
        UIDevice.current.playInputClick()
        switch key {
        case .character:
            state.pressed.insert(key)
            state.popup = key
        case .delete:
            state.pressed.insert(key)
            startDeleting()
        case .shift:
            shiftTapped()
        case .mouth:
            mouthDown()
        case .globe, .space, .returnKey, .pageToggle, .symbolsToggle:
            state.pressed.insert(key)
        }
    }

    func keyMoved(from old: KeyboardKey, to new: KeyboardKey) {
        state.pressed.remove(old)
        state.pressed.insert(new)
        state.popup = new
    }

    func keyUp(_ key: KeyboardKey) {
        release(key)
        switch key {
        case .character(let character):
            typeCharacter(character)
        case .delete:
            stopDeleting()
        case .space:
            typeSpace()
        case .returnKey:
            typeReturn()
        case .pageToggle:
            switchPage(state.page == .letters ? .numbers : .letters)
        case .symbolsToggle:
            switchPage(state.page == .numbers ? .symbols : .numbers)
        case .mouth:
            mouthUp()
        case .shift, .globe:
            break
        }
    }

    func keyCancelled(_ key: KeyboardKey) {
        release(key)
        switch key {
        case .delete: stopDeleting()
        case .mouth: endHold()
        case .space: cursorDragEnded()
        default: break
        }
    }

    // MARK: The cursor (the space bar as a trackpad)

    func cursorDragBegan() {
        resetCursorTrack()
        lastCorrection = nil
        autoSpace = false
        lastSpaceAt = nil
        dismissHistory()
        state.pressed = []
        state.popup = nil
        state.trackpad = true
        UIImpactFeedbackGenerator(style: .light).impactOccurred()  // with Full Access only
    }

    private func cursorDragEnded() {
        resetCursorTrack()
        guard state.trackpad else { return }
        state.trackpad = false
        refreshTyping()
    }

    private func resetCursorTrack() {
        cursorReread?.cancel()
        cursorReread = nil
        cursorText = nil
        cursorGoalX = nil
        cursorQueue = (0, 0)
    }

    func moveCursor(by characters: Int) {
        guard cursorReread == nil else {
            cursorQueue.characters += characters
            return
        }
        let text = cursorTextNow()
        let target = cursorPosition + characters
        cursorGoalX = nil
        if (0...text.characterCount).contains(target) {
            adjustCursor(to: target, in: text)
        } else {
            // Past the text it knows: to its end, on from there, and read the text again.
            let edge = target < 0 ? 0 : text.characterCount
            adjustCursor(to: edge, in: text)
            textDocumentProxy.adjustTextPosition(byCharacterOffset: target - edge)
            rereadCursorText(crossing: 0)
        }
    }

    func moveCursor(lines: Int) {
        guard cursorReread == nil else {
            cursorQueue.lines += lines
            return
        }
        let text = cursorTextNow()
        let goal = cursorGoalX ?? text.x(of: cursorPosition)
        cursorGoalX = goal
        let line = text.line(of: cursorPosition)
        let target = line + lines
        if (0..<text.lineCount).contains(target) {
            adjustCursor(to: text.position(onLine: target, nearestX: goal), in: text)
            return
        }
        // Past the first or last line it knows: to that end, one step on (over the line
        // break), and the rest once the text there has been read.
        let up = target < 0
        adjustCursor(to: up ? 0 : text.characterCount, in: text)
        textDocumentProxy.adjustTextPosition(byCharacterOffset: up ? -1 : 1)
        let used = up ? -(line + 1) : text.lineCount - line
        cursorQueue.lines += lines - used
        rereadCursorText(crossing: up ? -1 : 1)
    }

    /// The text around the cursor, laid out as the text field probably does: its width isn't
    /// told to keyboards, and three quarters of the screen sits between a chat's message box
    /// and a full-width page.
    private func cursorTextNow() -> KeyboardTextLines {
        if let cursorText { return cursorText }
        let proxy = textDocumentProxy
        let before = Array(proxy.documentContextBeforeInput ?? "")
        let after = Array(proxy.documentContextAfterInput ?? "")
        let font = UIFont.preferredFont(forTextStyle: .body)
        var widths: [Character: CGFloat] = [:]
        let text = KeyboardTextLines(before + after, width: max(160, view.bounds.width * 0.75)) { character in
            if let known = widths[character] { return known }
            let width = (String(character) as NSString).size(withAttributes: [.font: font]).width
            widths[character] = width
            return width
        }
        cursorText = text
        cursorPosition = before.count
        return text
    }

    private func adjustCursor(to position: Int, in text: KeyboardTextLines) {
        guard position != cursorPosition else { return }
        textDocumentProxy.adjustTextPosition(byCharacterOffset: text.utf16Distance(from: cursorPosition, to: position))
        cursorPosition = position
    }

    /// After stepping past the text it knew: once the field has caught up, reads the text there,
    /// puts the cursor where up or down aimed on its new line (`crossing`: -1 up, 1 down; not
    /// at the very start or end of the text), then makes the moves that came in meanwhile.
    private func rereadCursorText(crossing: Int) {
        cursorText = nil
        cursorReread = Task { [weak self] in
            try? await Task.sleep(for: Self.contextSettle)
            guard let self, !Task.isCancelled else { return }
            self.cursorReread = nil
            let text = self.cursorTextNow()
            let atEnd = crossing < 0 ? self.cursorPosition == 0 : self.cursorPosition == text.characterCount
            if crossing != 0, !atEnd, let goal = self.cursorGoalX {
                let line = text.line(of: self.cursorPosition)
                self.adjustCursor(to: text.position(onLine: line, nearestX: goal), in: text)
            }
            let queued = self.cursorQueue
            self.cursorQueue = (0, 0)
            if queued.characters != 0 { self.moveCursor(by: queued.characters) }
            if queued.lines != 0 { self.moveCursor(lines: queued.lines) }
        }
    }

    func globeTouch(from view: UIView, with event: UIEvent?) {
        guard let event else { return }
        handleInputModeList(from: view, with: event)
    }

    func advanceToNextKeyboard() {
        advanceToNextInputMode()
    }

    func spokenName(for key: KeyboardKey) -> String {
        switch key {
        case .character(let character): return state.label(for: character)
        case .shift: return state.shift == .locked ? "caps lock" : "shift"
        case .delete: return "delete"
        case .pageToggle: return state.page == .letters ? "numbers" : "letters"
        case .symbolsToggle: return state.page == .numbers ? "symbols" : "numbers"
        case .globe: return "next keyboard"
        case .mouth: return "Dictate"
        case .space: return "space"
        case .returnKey: return state.returnLabel
        }
    }

    private func release(_ key: KeyboardKey) {
        state.pressed.remove(key)
        if state.popup == key { state.popup = nil }
    }

    // MARK: Typing

    private func typeCharacter(_ character: String) {
        dismissHistory()
        let proxy = textDocumentProxy
        let text = state.page == .letters && state.shift != .off ? character.uppercased() : character
        let punctuation = ".,!?;:".contains(character)
        if punctuation, autoSpace, proxy.documentContextBeforeInput?.hasSuffix(" ") == true {
            proxy.deleteBackward()  // "word " from a suggestion + "," -> "word,"
        }
        if punctuation, !KeyboardTyping.currentWord(before: proxy.documentContextBeforeInput).isEmpty {
            correctWord(thenType: text)  // "teh." -> "the."
        } else {
            proxy.insertText(text)
            lastCorrection = nil
        }
        autoSpace = false
        lastSpaceAt = nil
        if state.page == .letters { setShift(KeyboardTyping.shiftAfterCharacter(state.shift)) }
        if state.page != .letters, character == "'" { switchPage(.letters) }
        refreshTyping()
    }

    private func typeSpace() {
        dismissHistory()
        let proxy = textDocumentProxy
        let now = Date()
        if let last = lastSpaceAt, now.timeIntervalSince(last) < KeyboardTyping.doubleSpaceWindow,
           KeyboardTyping.doubleSpaceMakesPeriod(before: proxy.documentContextBeforeInput) {
            proxy.deleteBackward()
            proxy.insertText(". ")
            lastCorrection = nil
            lastSpaceAt = nil
        } else {
            correctWord(thenType: " ")
            lastSpaceAt = now
        }
        autoSpace = false
        if state.page != .letters { switchPage(.letters) }
        refreshTyping()
    }

    private func typeReturn() {
        dismissHistory()
        correctWord(thenType: "\n")
        autoSpace = false
        lastSpaceAt = nil
        if state.page != .letters { switchPage(.letters) }
        refreshTyping()
    }

    private func shiftTapped() {
        let now = Date()
        setShift(KeyboardTyping.tapShift(state.shift, lastTapAt: lastShiftTap, now: now))
        lastShiftTap = state.shift == .locked ? nil : now
    }

    private func suggestionTapped(_ suggestion: KeyboardSuggestion) {
        dismissHistory()
        let proxy = textDocumentProxy
        let word = KeyboardTyping.currentWord(before: proxy.documentContextBeforeInput)
        for _ in 0..<word.count { proxy.deleteBackward() }
        proxy.insertText(suggestion.text + " ")
        if suggestion.kind == .typed { UITextChecker.learnWord(suggestion.text) }
        lastCorrection = nil
        autoSpace = true
        lastSpaceAt = Date()
        refreshTyping()
    }

    // MARK: Delete (hold to repeat, then whole words)

    private func startDeleting() {
        deleteOnce()
        deleteRepeat?.cancel()
        deleteRepeat = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(450))
            var ticks = 0
            while !Task.isCancelled {
                guard let self else { return }
                let proxy = self.textDocumentProxy
                if ticks >= 18 {
                    let count = KeyboardTyping.wordDeletionLength(before: proxy.documentContextBeforeInput)
                    for _ in 0..<count { proxy.deleteBackward() }
                    self.refreshTyping()
                    try? await Task.sleep(for: .milliseconds(260))
                } else {
                    proxy.deleteBackward()
                    self.refreshTyping()
                    try? await Task.sleep(for: .milliseconds(80))
                }
                ticks += 1
            }
        }
    }

    /// One delete; right after an automatic correction it puts back what you typed instead
    /// (and stops correcting that word).
    private func deleteOnce() {
        dismissHistory()
        let proxy = textDocumentProxy
        if let fix = lastCorrection, proxy.documentContextBeforeInput?.hasSuffix(fix.replacement + fix.suffix) == true {
            for _ in 0..<(fix.replacement.count + fix.suffix.count) { proxy.deleteBackward() }
            proxy.insertText(fix.original)
            UITextChecker.learnWord(fix.original)
        } else {
            proxy.deleteBackward()
        }
        lastCorrection = nil
        autoSpace = false
        lastSpaceAt = nil
        refreshTyping()
    }

    private func stopDeleting() {
        deleteRepeat?.cancel()
        deleteRepeat = nil
    }

    // MARK: Notices and Undo

    private func showNotice(_ text: String) {
        state.notice = text
        noticeEnd?.cancel()
        noticeEnd = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard let self, !Task.isCancelled else { return }
            self.state.notice = nil
        }
    }

    private func dismissHistory() {
        if state.history != nil { state.history = nil }
    }

    // MARK: The app's state

    private func received(_ signal: KeyboardSignal) {
        guard visible else { return }
        switch signal {
        case .state, .result:
            refresh()
        case .start, .stop, .cancel, .hidden:
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
        if visible {
            // Every second while up: the app keeps its session only while a keyboard is.
            store.keyboardOnScreenUntil = now.addingTimeInterval(KeyboardSessionPolicy.onScreenValidity)
        }
        if let since = startPendingSince {
            switch appState?.phase(at: now) ?? .idle {
            case .listening, .working:
                startPendingSince = nil  // it answered
            case .idle, .failed:
                if KeyboardStartWait.shouldOpenApp(pendingSince: since, app: appState, now: now) {
                    startPendingSince = nil
                    endHold()
                    openApp()
                }
            }
        }
        updateMouth()
        typeWaitingDictation()
    }

    private func updateMouth() {
        let now = Date()
        var mouth = KeyboardMouthState.make(
            app: hasFullAccess ? appState : nil,
            startPending: startPendingSince != nil,
            now: now
        )
        if let until = panelHiddenUntil {
            if now >= until || mouth == .idle || mouth == .failed {
                panelHiddenUntil = nil
            } else if mouth.isListening {
                mouth = .idle  // cancelled: the app hasn't said so yet
            }
        }
        if mouth == .failed, let since = appState?.since, failureShownFor != since {
            failureShownFor = since
            showNotice("That didn't work. Tap the mouth to try again.")
        }
        guard state.mouth != mouth else { return }
        let hadPanel = state.showsPanel
        state.mouth = mouth
        if hadPanel != state.showsPanel {
            state.popup = nil
            state.pressed = []
            surface.update(frames: state.keyFrames, keyAreaTop: state.barHeight, panelMode: state.showsPanel)
            if !state.showsPanel { refreshTyping() }
        }
    }

    // MARK: The mouth

    private func mouthDown() {
        guard hasFullAccess else {
            showNotice("To dictate, turn on Allow Full Access: Settings › Apps › ShoutAbout › Keyboards.")
            return
        }
        panelHiddenUntil = nil
        dismissHistory()
        appState = store.appState
        let now = Date()
        switch KeyboardMouthAction.decide(hasFullAccess: true, app: appState, startPending: startPendingSince != nil, now: now) {
        case .signalStart:
            startPendingSince = now
            KeyboardSignals.post(.start)
            beginHold(at: now)
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

    private func beginHold(at now: Date) {
        holdStartedAt = now
        holdTimer?.cancel()
        holdTimer = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.holdThreshold))
            guard let self, !Task.isCancelled, self.holdStartedAt != nil else { return }
            self.state.holding = true
        }
    }

    /// A quick tap leaves it listening (tap anywhere to finish); after a hold, letting go
    /// finishes.
    private func mouthUp() {
        let held = holdStartedAt.map { Date().timeIntervalSince($0) >= Self.holdThreshold } ?? false
        endHold()
        if held { finishDictation() }
    }

    private func endHold() {
        holdStartedAt = nil
        holdTimer?.cancel()
        holdTimer = nil
        if state.holding { state.holding = false }
    }

    private func finishDictation() {
        appState = store.appState
        switch KeyboardMouthAction.decide(hasFullAccess: hasFullAccess, app: appState, startPending: startPendingSince != nil, now: Date()) {
        case .signalStop:
            startPendingSince = nil
            KeyboardSignals.post(.stop)
        case .signalStart, .openApp, .ignore:
            break  // not listening (any more)
        }
        updateMouth()
    }

    func panelTapped() {
        guard state.showsPanel, state.mouth != .working else { return }
        endHold()
        finishDictation()
    }

    func panelCancelTapped() {
        guard state.showsPanel, state.mouth != .working else { return }
        endHold()
        startPendingSince = nil
        KeyboardSignals.post(.cancel)
        panelHiddenUntil = Date().addingTimeInterval(2)
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
            // On the clipboard too (Full Access allows it), like a phone dictation from
            // anywhere else: the app, in the background, couldn't (and so doesn't come forward
            // for it either).
            if self.hasFullAccess { UIPasteboard.general.string = handoff.text }
        }
    }

    private func type(_ text: String) {
        let proxy = textDocumentProxy
        let typed = KeyboardText.insertion(text, after: proxy.documentContextBeforeInput)
        proxy.insertText(typed)
        lastInsertion = typed
        state.history = .undo
        lastCorrection = nil
        autoSpace = false
        lastSpaceAt = nil
        refreshTyping()
    }

    /// Undo takes the dictation back out (only while it's right before the cursor); Redo puts
    /// it back. Neither touches the clipboard.
    private func historyTapped() {
        guard let typed = lastInsertion, let history = state.history else { return }
        let proxy = textDocumentProxy
        switch history {
        case .undo:
            guard KeyboardTyping.isJustBeforeCursor(typed, before: proxy.documentContextBeforeInput) else {
                state.history = nil  // the cursor moved away from it
                return
            }
            for _ in 0..<typed.count { proxy.deleteBackward() }
            state.history = .redo
        case .redo:
            proxy.insertText(typed)
            state.history = .undo
        }
        lastCorrection = nil
        autoSpace = false
        lastSpaceAt = nil
        refreshTyping()
    }
}

/// The keyboard's main view: asks iOS for keyboard clicks (they follow Settings › Sounds).
final class KeyboardInputView: UIInputView, UIInputViewAudioFeedback {
    var enableInputClicksWhenVisible: Bool { true }
}
