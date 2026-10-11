import XCTest

/// A screenshot tour of the app with made-up sample data (Debug-only `-ScreenshotScene`
/// launch argument, see AppModel.applyScreenshotScene). Each screenshot is an XCTAttachment
/// named after the scene; CI exports them from the .xcresult into the `ios-screenshots`
/// artifact. Not a functional test: it only fails if the app can't launch.
///
/// Tests run in name order, so the Lock Screen one (which leaves the phone locked) is last.
final class ScreenshotTests: XCTestCase {
    private var springboard: XCUIApplication {
        XCUIApplication(bundleIdentifier: "com.apple.springboard")
    }

    override func setUpWithError() throws {
        continueAfterFailure = true
    }

    // MARK: Helpers

    @discardableResult
    private func launch(_ scene: String, dark: Bool = false, settle: TimeInterval = 2.5) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ScreenshotScene", scene, "-ScreenshotAppearance", dark ? "dark" : "light"]
        app.launch()
        pause(settle)
        return app
    }

    private func snap(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func note(_ name: String, _ text: String) {
        let attachment = XCTAttachment(string: text)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func pause(_ seconds: TimeInterval) {
        Thread.sleep(forTimeInterval: seconds)
    }

    private func goHome() {
        XCUIDevice.shared.press(.home)
        pause(2.5)
    }

    /// Long-press the Dynamic Island to show the expanded Live Activity.
    private func expandIsland() {
        let island = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0))
            .withOffset(CGVector(dx: 0, dy: 30))
        island.press(forDuration: 1.2)
        pause(1.5)
    }

    // MARK: Onboarding

    func test01Onboarding() {
        launch("onboarding")
        snap("light-01-onboarding-choice")
        launch("onboarding", dark: true)
        snap("dark-01-onboarding-choice")
    }

    func test02Discovery() {
        launch("discovery")
        snap("light-02-discovery-list")
    }

    func test03PairingCode() {
        launch("pairing-code")
        snap("light-03-pairing-click-allow")
        launch("pairing-code", dark: true)
        snap("dark-03-pairing-click-allow")
    }

    func test04ActionButton() {
        launch("action-button")
        snap("light-04-action-button-card")
    }

    // MARK: Main screen

    func test05MainComputer() {
        launch("main-computer")
        snap("light-05-main-computer")
        launch("main-computer", dark: true)
        snap("dark-05-main-computer")
    }

    func test06MainPhone() {
        launch("main-phone")
        snap("light-06-main-phone")
        launch("main-phone", dark: true)
        snap("dark-06-main-phone")
    }

    func test07Recording() {
        launch("recording")
        snap("light-07-recording")
        launch("recording", dark: true)
        snap("dark-07-recording")
    }

    func test08Processing() {
        launch("processing")
        snap("light-08-sending")
    }

    func test09Result() {
        launch("result")
        snap("light-09-result-card")
        launch("result", dark: true)
        snap("dark-09-result-card")
    }

    func test10Away() {
        launch("away")
        snap("light-10-away-clipboard")
    }

    func test11Failed() {
        launch("failed")
        snap("light-11-failed")
    }

    func test12Sheets() {
        launch("settings")
        snap("light-12-settings")
        launch("settings", dark: true)
        snap("dark-12-settings")
        launch("history")
        snap("light-13-history")
    }

    // MARK: Live Activity (the mouth pop-up)

    func test20IslandListening() {
        launch("island-listening", settle: 3)
        goHome()
        snap("island-21-compact-listening-home")
        expandIsland()
        snap("island-22-expanded-listening")
        note("island-22-springboard-tree", springboard.debugDescription)
        goHome()
    }

    func test21IslandSending() {
        launch("island-sending", settle: 3)
        goHome()
        snap("island-23-compact-sending-home")
    }

    func test22IslandDone() {
        launch("island-done", settle: 3)
        goHome()
        snap("island-24-compact-pasted-home")
        expandIsland()
        snap("island-25-expanded-pasted")
        goHome()
    }

    /// The app updates its activity with an alert ~6 s after launch, which makes iOS expand
    /// the island by itself: a second way to capture the expanded look.
    func test23IslandAlert() {
        launch("island-alert", settle: 2)
        goHome()
        for index in 1...5 {
            pause(1)
            snap("island-26-alert-\(index)")
        }
    }

    // MARK: Lock Screen (last: it leaves the simulator locked)

    func test99LockScreen() {
        launch("island-listening", settle: 3)
        goHome()
        let lock = NSSelectorFromString("pressLockButton")
        guard XCUIDevice.shared.responds(to: lock) else {
            note("lock-screen-unavailable", "XCUIDevice has no pressLockButton on this Xcode.")
            return
        }
        XCUIDevice.shared.perform(lock)
        pause(2)
        snap("lock-31-after-lock")
        // Wake the display without unlocking (tap to wake), then look again.
        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        pause(2)
        snap("lock-32-tap-to-wake")
        XCUIDevice.shared.press(.home)
        pause(2)
        snap("lock-33-after-home")
        // The first time, iOS asks "Allow Live Activities from ShoutAbout?" on the Lock Screen
        // (as it will on a real iPhone). Accept it for this test app, then look again.
        let allow = springboard.buttons["Allow"]
        if allow.waitForExistence(timeout: 2) {
            allow.tap()
            pause(2)
            snap("lock-34-after-allowing-live-activities")
        } else {
            note("lock-34-no-prompt", "No Live Activities prompt was on screen.")
        }
    }
}
