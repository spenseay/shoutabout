import XCTest

/// The ShoutAbout keyboard <-> app link (Shared/Core/KeyboardBridge.swift): what a tap on the
/// keyboard's mouth does, what it shows, which dictations it types, and the once-only claim.
final class KeyboardBridgeTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    /// A throwaway defaults suite, removed when the test ends.
    private func makeStore() -> KeyboardLinkStore {
        let suite = "shoutabout-keyboard-tests-\(UUID().uuidString)"
        addTeardownBlock {
            UserDefaults.standard.removePersistentDomain(forName: suite)
        }
        return KeyboardLinkStore(defaults: UserDefaults(suiteName: suite) ?? .standard)
    }

    private func app(_ phase: KeyboardAppState.Phase, session: Bool, writtenAgo: TimeInterval = 0, phaseAgo: TimeInterval = 0) -> KeyboardAppState {
        .heartbeat(
            phase: phase,
            since: now.addingTimeInterval(-phaseAgo),
            sessionOpen: session,
            now: now.addingTimeInterval(-writtenAgo)
        )
    }

    // MARK: Signals

    func testSignalNamesAreUnderTheKeyboardPrefix() {
        XCTAssertEqual(KeyboardSignal.allCases.count, 4)
        for signal in KeyboardSignal.allCases {
            XCTAssertTrue(signal.rawValue.hasPrefix("fyi.shoutabout.keyboard."), signal.rawValue)
        }
        XCTAssertEqual(Set(KeyboardSignal.allCases.map(\.rawValue)).count, 4)
    }

    // MARK: The app's heartbeat

    func testTheAppStateExpiresWithoutAHeartbeat() {
        let fresh = app(.listening, session: true)
        XCTAssertEqual(fresh.phase(at: now), .listening)
        XCTAssertTrue(fresh.sessionIsAlive(at: now))
        XCTAssertTrue(fresh.sessionIsAlive(at: now.addingTimeInterval(KeyboardAppState.validity)))

        let stale = app(.listening, session: true, writtenAgo: KeyboardAppState.validity + 1)
        XCTAssertEqual(stale.phase(at: now), .idle)
        XCTAssertFalse(stale.sessionIsAlive(at: now))
        // The app writes it more often than it expires.
        XCTAssertLessThan(KeyboardAppState.refreshInterval * 2, KeyboardAppState.validity)
    }

    func testNoSessionIsNoSessionEvenWhileAlive() {
        XCTAssertFalse(app(.idle, session: false).sessionIsAlive(at: now))
    }

    func testAppStateRoundTripsThroughTheStore() {
        let store = makeStore()
        XCTAssertNil(store.appState)
        let state = KeyboardAppState(
            phase: .working,
            since: Date(timeIntervalSince1970: 1_000),
            sessionOpen: true,
            aliveUntil: Date(timeIntervalSince1970: 1_025)
        )
        store.appState = state
        XCTAssertEqual(KeyboardLinkStore(defaults: store.defaults).appState, state)
        store.appState = nil
        XCTAssertNil(store.appState)
    }

    // MARK: A tap on the mouth

    func testTapWithASessionStartsRightAway() {
        XCTAssertEqual(
            KeyboardMouthAction.decide(hasFullAccess: true, app: app(.idle, session: true), startPending: false, now: now),
            .signalStart
        )
    }

    func testTapWithoutASessionOpensTheApp() {
        XCTAssertEqual(KeyboardMouthAction.decide(hasFullAccess: true, app: nil, startPending: false, now: now), .openApp)
        XCTAssertEqual(
            KeyboardMouthAction.decide(hasFullAccess: true, app: app(.idle, session: false), startPending: false, now: now),
            .openApp
        )
        // A session whose heartbeat stopped (the app was stopped) is no session.
        let stale = app(.idle, session: true, writtenAgo: 60)
        XCTAssertEqual(KeyboardMouthAction.decide(hasFullAccess: true, app: stale, startPending: false, now: now), .openApp)
    }

    func testTapWhileListeningStops() {
        // Whatever started it (the keyboard, the Action Button, the app's own button).
        for session in [true, false] {
            XCTAssertEqual(
                KeyboardMouthAction.decide(hasFullAccess: true, app: app(.listening, session: session), startPending: false, now: now),
                .signalStop
            )
        }
        // A second tap before the app answered the first.
        XCTAssertEqual(
            KeyboardMouthAction.decide(hasFullAccess: true, app: app(.idle, session: true), startPending: true, now: now),
            .signalStop
        )
    }

    func testTapWhileWorkingOrWithoutFullAccessDoesNothing() {
        XCTAssertEqual(
            KeyboardMouthAction.decide(hasFullAccess: true, app: app(.working, session: true), startPending: false, now: now),
            .ignore
        )
        XCTAssertEqual(
            KeyboardMouthAction.decide(hasFullAccess: false, app: app(.idle, session: true), startPending: false, now: now),
            .ignore
        )
        XCTAssertEqual(KeyboardMouthAction.decide(hasFullAccess: false, app: nil, startPending: false, now: now), .ignore)
    }

    func testTapAfterAFailureStartsAgain() {
        XCTAssertEqual(
            KeyboardMouthAction.decide(hasFullAccess: true, app: app(.failed, session: true), startPending: false, now: now),
            .signalStart
        )
    }

    // MARK: Waiting for the app to start

    func testAnUnansweredStartFallsBackToOpeningTheApp() {
        let asked = now
        let idle = app(.idle, session: true)
        XCTAssertFalse(KeyboardStartWait.shouldOpenApp(pendingSince: asked, app: idle, now: now.addingTimeInterval(1)))
        XCTAssertTrue(KeyboardStartWait.shouldOpenApp(pendingSince: asked, app: idle, now: now.addingTimeInterval(KeyboardStartWait.timeout)))
        XCTAssertTrue(KeyboardStartWait.shouldOpenApp(pendingSince: asked, app: nil, now: now.addingTimeInterval(5)))
        // It answered: no fallback.
        let listening = app(.listening, session: true)
        XCTAssertFalse(KeyboardStartWait.shouldOpenApp(pendingSince: asked, app: listening, now: now.addingTimeInterval(3)))
        XCTAssertFalse(KeyboardStartWait.shouldOpenApp(pendingSince: asked, app: app(.working, session: true), now: now.addingTimeInterval(3)))
    }

    // MARK: What the mouth shows

    func testMouthState() {
        XCTAssertEqual(KeyboardMouthState.make(app: nil, startPending: false, now: now), .idle)
        XCTAssertEqual(KeyboardMouthState.make(app: nil, startPending: true, now: now), .starting)
        XCTAssertEqual(KeyboardMouthState.make(app: app(.listening, session: true), startPending: true, now: now), .listening)
        XCTAssertEqual(KeyboardMouthState.make(app: app(.working, session: false), startPending: false, now: now), .working)
        XCTAssertEqual(KeyboardMouthState.make(app: app(.listening, session: true, writtenAgo: 60), startPending: false, now: now), .idle)
        XCTAssertTrue(KeyboardMouthState.starting.isListening)
        XCTAssertTrue(KeyboardMouthState.listening.isListening)
        XCTAssertFalse(KeyboardMouthState.working.isListening)
    }

    func testAFailureShowsOnlyBriefly() {
        XCTAssertEqual(KeyboardMouthState.make(app: app(.failed, session: true, phaseAgo: 1), startPending: false, now: now), .failed)
        XCTAssertEqual(
            KeyboardMouthState.make(app: app(.failed, session: true, phaseAgo: KeyboardAppState.failedShowsFor + 1), startPending: false, now: now),
            .idle
        )
        XCTAssertEqual(KeyboardMouthState.make(app: app(.failed, session: true, phaseAgo: 1), startPending: true, now: now), .starting)
    }

    // MARK: Which dictations the keyboard types

    func testOnlyPhoneDictationsWithWordsAreHandedOver() {
        XCTAssertTrue(KeyboardHandoff.shouldPublish(toPhone: true, text: "Sample text: call Sofia.", keyboardSetUp: true))
        XCTAssertFalse(KeyboardHandoff.shouldPublish(toPhone: false, text: "Sample text: typed on the PC.", keyboardSetUp: true))
        XCTAssertFalse(KeyboardHandoff.shouldPublish(toPhone: true, text: "", keyboardSetUp: true))
        XCTAssertFalse(KeyboardHandoff.shouldPublish(toPhone: true, text: "  \n", keyboardSetUp: true))
        // No keyboard: nothing waits in the App Group.
        XCTAssertFalse(KeyboardHandoff.shouldPublish(toPhone: true, text: "Sample text: call Sofia.", keyboardSetUp: false))
    }

    func testAutoInsertWindowAndExpiry() {
        let handoff = KeyboardHandoff(id: "a", text: "Sample text", createdAt: now)
        XCTAssertTrue(handoff.canAutoInsert(at: now))
        XCTAssertTrue(handoff.canAutoInsert(at: now.addingTimeInterval(KeyboardHandoff.insertWindow)))
        XCTAssertFalse(handoff.canAutoInsert(at: now.addingTimeInterval(KeyboardHandoff.insertWindow + 1)))
        XCTAssertFalse(handoff.isExpired(at: now.addingTimeInterval(KeyboardHandoff.insertWindow + 1)))
        XCTAssertTrue(handoff.isExpired(at: now.addingTimeInterval(KeyboardHandoff.maxAge + 1)))
        // From the future (the clock changed): not trusted.
        XCTAssertFalse(handoff.canAutoInsert(at: now.addingTimeInterval(-120)))
        XCTAssertTrue(handoff.isExpired(at: now.addingTimeInterval(-120)))
        XCTAssertFalse(KeyboardHandoff(id: "b", text: "", createdAt: now).canAutoInsert(at: now))
        // Kept a few minutes at most.
        XCTAssertLessThanOrEqual(KeyboardHandoff.maxAge, 5 * 60)
        XCTAssertLessThan(KeyboardHandoff.insertWindow, KeyboardHandoff.maxAge)
    }

    func testOnlyTheLatestHandoffIsKeptAndExpiredOnesAreRemoved() {
        let store = makeStore()
        XCTAssertNil(store.handoff(now: now))
        store.publish(KeyboardHandoff(id: "first", text: "one", createdAt: now))
        store.publish(KeyboardHandoff(id: "second", text: "two", createdAt: now))
        XCTAssertEqual(store.handoff(now: now)?.id, "second")
        XCTAssertNil(store.handoff(now: now.addingTimeInterval(KeyboardHandoff.maxAge + 1)))
        XCTAssertNil(store.defaults.data(forKey: KeyboardLinkStore.Key.handoff))  // gone from the App Group
    }

    func testRemovingOnlyTheHandoffThatWasTyped() {
        let store = makeStore()
        store.publish(KeyboardHandoff(id: "new", text: "newer", createdAt: now))
        store.removeHandoff(id: "old")  // typed an older one meanwhile: the newer one stays
        XCTAssertEqual(store.handoff(now: now)?.id, "new")
        store.removeHandoff(id: "new")
        XCTAssertNil(store.handoff(now: now))
        store.publish(KeyboardHandoff(id: "x", text: "x", createdAt: now))
        store.removeHandoff()
        XCTAssertNil(store.handoff(now: now))
    }

    // MARK: Typed once

    func testTheFirstClaimWins() {
        let store = makeStore()
        let handoff = KeyboardHandoff(id: "dictation-1", text: "Sample text", createdAt: now)
        XCTAssertTrue(store.claim(handoff, by: "keyboard-a"))
        XCTAssertTrue(store.holdsClaim(handoff, by: "keyboard-a"))
        // Another keyboard, and the same one a second time: no.
        XCTAssertFalse(store.claim(handoff, by: "keyboard-b"))
        XCTAssertFalse(store.holdsClaim(handoff, by: "keyboard-b"))
        XCTAssertFalse(store.claim(handoff, by: "keyboard-a"))
        // A new dictation can be claimed again.
        let next = KeyboardHandoff(id: "dictation-2", text: "More", createdAt: now)
        XCTAssertTrue(store.claim(next, by: "keyboard-b"))
        XCTAssertFalse(store.holdsClaim(next, by: "keyboard-a"))
        XCTAssertTrue(store.holdsClaim(next, by: "keyboard-b"))
    }

    func testAReleasedClaimCanBeTakenAgain() {
        let store = makeStore()
        let handoff = KeyboardHandoff(id: "dictation-4", text: "Sample text", createdAt: now)
        XCTAssertTrue(store.claim(handoff, by: "keyboard-a"))
        store.releaseClaim(handoff, by: "keyboard-b")  // not b's to give back
        XCTAssertTrue(store.holdsClaim(handoff, by: "keyboard-a"))
        store.releaseClaim(handoff, by: "keyboard-a")
        XCTAssertTrue(store.claim(handoff, by: "keyboard-b"))
        XCTAssertTrue(store.holdsClaim(handoff, by: "keyboard-b"))
    }

    func testSimultaneousClaimsAgreeOnOneWinner() {
        // Both checked before either wrote; the last write stands for both readers.
        let store = makeStore()
        let handoff = KeyboardHandoff(id: "dictation-3", text: "Sample text", createdAt: now)
        store.defaults.set(KeyboardClaim(id: handoff.id, claimant: "keyboard-a").stored, forKey: KeyboardLinkStore.Key.claim)
        store.defaults.set(KeyboardClaim(id: handoff.id, claimant: "keyboard-b").stored, forKey: KeyboardLinkStore.Key.claim)
        XCTAssertFalse(store.holdsClaim(handoff, by: "keyboard-a"))
        XCTAssertTrue(store.holdsClaim(handoff, by: "keyboard-b"))
    }

    func testClaimEncoding() {
        let claim = KeyboardClaim(id: "6F9619FF-8B86-D011-B42D-00C04FC964FF", claimant: "A1B2")
        XCTAssertEqual(KeyboardClaim(stored: claim.stored), claim)
        XCTAssertNil(KeyboardClaim(stored: ""))
        XCTAssertNil(KeyboardClaim(stored: "only-an-id"))
        XCTAssertNil(KeyboardClaim(stored: " claimant"))
    }

    // MARK: Spacing at the cursor

    func testSpacingAtTheCursor() {
        XCTAssertEqual(KeyboardText.insertion("call Sofia", after: nil), "call Sofia")
        XCTAssertEqual(KeyboardText.insertion("call Sofia", after: ""), "call Sofia")
        XCTAssertEqual(KeyboardText.insertion("call Sofia", after: "Remind me to"), " call Sofia")
        XCTAssertEqual(KeyboardText.insertion("Then lunch.", after: "Meeting at ten."), " Then lunch.")
        XCTAssertEqual(KeyboardText.insertion("call Sofia", after: "Remind me to "), "call Sofia")
        XCTAssertEqual(KeyboardText.insertion("call Sofia", after: "Notes:\n"), "call Sofia")
        XCTAssertEqual(KeyboardText.insertion("call Sofia", after: "("), "call Sofia")
        XCTAssertEqual(KeyboardText.insertion(", and Sofia", after: "Call Marc"), ", and Sofia")
        XCTAssertEqual(KeyboardText.insertion("", after: "Call"), "")
    }

    // MARK: The session

    func testTheSessionClosesAfterFiveIdleMinutes() {
        let lastUse = now
        XCTAssertFalse(KeyboardSessionPolicy.shouldClose(lastUse: lastUse, now: now.addingTimeInterval(299), dictating: false))
        XCTAssertTrue(KeyboardSessionPolicy.shouldClose(lastUse: lastUse, now: now.addingTimeInterval(300), dictating: false))
        XCTAssertFalse(KeyboardSessionPolicy.shouldClose(lastUse: lastUse, now: now.addingTimeInterval(900), dictating: true))
        XCTAssertEqual(KeyboardSessionPolicy.idleTimeout, 5 * 60)
    }

    // MARK: Setup

    func testKeyboardCountsAsSetUpOnceItRanWithFullAccess() {
        let store = makeStore()
        XCTAssertFalse(store.keyboardIsSetUp)
        store.fullAccessSeenAt = now
        XCTAssertTrue(KeyboardLinkStore(defaults: store.defaults).keyboardIsSetUp)
        XCTAssertEqual(store.fullAccessSeenAt, now)
        store.fullAccessSeenAt = nil
        XCTAssertFalse(store.keyboardIsSetUp)
    }
}
