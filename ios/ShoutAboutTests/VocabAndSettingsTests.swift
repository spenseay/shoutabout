import XCTest

final class VocabFixerTests: XCTestCase {
    func testCanonicalSpelling() {
        let terms = ["Better Shell", "ShoutAbout", "GPU"]
        XCTAssertEqual(
            VocabFixer.apply("open better shell and ask shout about the gpu", terms: terms),
            "open Better Shell and ask ShoutAbout the GPU"
        )
        XCTAssertEqual(VocabFixer.apply("bettershell, Better-Shell.", terms: terms), "Better Shell, Better Shell.")
    }

    func testWordBoundaries() {
        XCTAssertEqual(VocabFixer.apply("gpus and gpu", terms: ["GPU"]), "gpus and GPU")
        XCTAssertEqual(VocabFixer.apply("nothing here", terms: []), "nothing here")
        XCTAssertEqual(VocabFixer.apply("", terms: ["X"]), "")
    }

    func testLongestTermWins() {
        XCTAssertEqual(
            VocabFixer.apply("better shell pro rocks", terms: ["Better Shell", "Better Shell Pro"]),
            "Better Shell Pro rocks"
        )
    }

    func testCamelCaseSplit() {
        XCTAssertEqual(VocabFixer.splitCamelCase("ShoutAbout"), ["Shout", "About"])
        XCTAssertEqual(VocabFixer.splitCamelCase("GPU"), ["GPU"])
        XCTAssertEqual(VocabFixer.splitCamelCase("iPhone"), ["i", "Phone"])
    }

    func testTemplateCharactersAreLiteral() {
        XCTAssertEqual(VocabFixer.apply("pay us 5 dollars", terms: ["$5 Dollars"]), "pay us 5 dollars")
        XCTAssertEqual(VocabFixer.apply("price is c sharp", terms: ["C\\Sharp"]), "price is c sharp")
    }
}

/// The sound-alike pass: a custom spelling the recognizer doesn't know still sticks.
final class VocabSoundAlikeTests: XCTestCase {
    /// A realistic word list (the owner's words plus a few more).
    private let vocab = ["Sofia", "Better Shell", "ShoutAbout", "TestFlight", "Sample Project", "faster-whisper", "Claude", "GPU"]

    func testSophiaBecomesSofia() {
        XCTAssertEqual(VocabFixer.apply("I talked to Sophia today.", terms: ["Sofia"]), "I talked to Sofia today.")
        XCTAssertEqual(VocabFixer.apply("Sophia's idea was great", terms: ["Sofia"]), "Sofia's idea was great")
        XCTAssertEqual(VocabFixer.apply("Sophia called. Then Sophia left", terms: vocab), "Sofia called. Then Sofia left")
    }

    func testBetterShell() {
        XCTAssertEqual(VocabFixer.apply("open better shell now", terms: vocab), "open Better Shell now")
        XCTAssertEqual(VocabFixer.apply("open bettor shell now", terms: vocab), "open Better Shell now")
        XCTAssertEqual(VocabFixer.apply("Open Bettor Shell now", terms: vocab), "Open Better Shell now")
    }

    func testShoutAbout() {
        XCTAssertEqual(VocabFixer.apply("tell shout about", terms: vocab), "tell ShoutAbout")
        XCTAssertEqual(VocabFixer.apply("tell Shout Aboot about it", terms: vocab), "tell ShoutAbout about it")
        XCTAssertEqual(VocabFixer.apply("ask Shoutabaut", terms: vocab), "ask ShoutAbout")
        // Common words in lowercase are never rewritten by sound.
        XCTAssertEqual(VocabFixer.apply("we'll shoot about ten photos", terms: vocab), "we'll shoot about ten photos")
        XCTAssertEqual(VocabFixer.apply("tell shout aboot", terms: vocab), "tell shout aboot")
        // A different number of words is never a match.
        XCTAssertEqual(VocabFixer.apply("shout a boat", terms: vocab), "shout a boat")
    }

    func testOrdinarySentencesStayAlone() {
        let sentences = [
            "I sat on the sofa with my phone",
            "a simple project for the team",
            "the cloud is gray and we sell shells",
            "We'd better sell the shell, then test the flight.",
            "Cloud storage is cheap",
            "the gpus and the graphics",
        ]
        for sentence in sentences {
            XCTAssertEqual(VocabFixer.apply(sentence, terms: vocab), sentence)
        }
    }

    func testCommonWordsNeedACapitalInMidSentence() {
        // "Mark" starting a sentence is the verb; capitalised mid-sentence it's a name.
        XCTAssertEqual(VocabFixer.apply("Mark the date, then call Mark.", terms: ["Marc"]), "Mark the date, then call Marc.")
        XCTAssertEqual(VocabFixer.apply("I asked Cloud for help", terms: ["Claude"]), "I asked Claude for help")
    }

    func testLongestTermWins() {
        XCTAssertEqual(
            VocabFixer.apply("bettor shell pro rocks", terms: ["Better Shell", "Better Shell Pro"]),
            "Better Shell Pro rocks"
        )
        XCTAssertEqual(
            VocabFixer.apply("Sophia Copola directed it", terms: ["Sofia", "Sofia Coppola"]),
            "Sofia Coppola directed it"
        )
    }

    func testExactTermsAreNeverRewritten() {
        XCTAssertEqual(VocabFixer.apply("Sophia and Sofia", terms: ["Sophia", "Sofia"]), "Sophia and Sofia")
    }

    func testShortWordsAndSentenceBreaks() {
        // Terms under 4 letters only get the same-spelling pass.
        XCTAssertEqual(VocabFixer.apply("Zoey said hi", terms: ["Zoe"]), "Zoey said hi")
        // A term never spans a sentence break.
        XCTAssertEqual(VocabFixer.apply("Sophia.\nLoren", terms: ["Sofia Loren"]), "Sophia.\nLoren")
    }

    func testNamesAndLowercaseTerms() {
        XCTAssertEqual(VocabFixer.apply("Hi Phillip", terms: ["Philip"]), "Hi Philip")
        XCTAssertEqual(VocabFixer.apply("Hi Stephen", terms: ["Steven"]), "Hi Steven")
        XCTAssertEqual(VocabFixer.apply("Hi Jon", terms: ["John"]), "Hi John")
        XCTAssertEqual(VocabFixer.apply("use Postgress here", terms: ["postgres"]), "use postgres here")
        // A lowercase term starting a sentence keeps the capital.
        XCTAssertEqual(VocabFixer.apply("Faster wisper is fast", terms: vocab), "Faster-whisper is fast")
    }
}

/// Replacements ("say -> write" lines on the PC) and custom words with a symbol said aloud: the
/// same rules as the hub's shoutabout/vocabfix.py.
final class VocabReplacementTests: XCTestCase {
    private func rule(_ say: String, _ write: String) -> VocabReplacement {
        VocabReplacement(say: say, write: write)
    }

    func testLikePhoneTextReplacement() {
        let rules = [rule("omw", "on my way")]
        XCTAssertEqual(VocabFixer.apply("omw, see you soon", terms: [], replacements: rules), "on my way, see you soon")
        XCTAssertEqual(VocabFixer.apply("Omw.", terms: [], replacements: rules), "On my way.")
        XCTAssertEqual(VocabFixer.apply("OMW", terms: [], replacements: rules), "On my way")
        XCTAssertEqual(VocabFixer.apply("zomwz and omwhere", terms: [], replacements: rules), "zomwz and omwhere")
    }

    func testSpokenPhrases() {
        let rules = [rule("slash consolidate", "/consolidate")]
        XCTAssertEqual(VocabFixer.apply("Slash consolidate.", terms: [], replacements: rules), "/consolidate.")
        XCTAssertEqual(VocabFixer.apply("run slash, consolidate now", terms: [], replacements: rules), "run /consolidate now")
        XCTAssertEqual(VocabFixer.apply("please consolidate it", terms: [], replacements: rules), "please consolidate it")
    }

    func testOwnCapitalsAndLongestFirst() {
        XCTAssertEqual(VocabFixer.apply("my eye phone", terms: [], replacements: [rule("eye phone", "iPhone")]), "my iPhone")
        let rules = [rule("c sharp", "C#"), rule("c sharp code", "C# source")]
        XCTAssertEqual(VocabFixer.apply("write c sharp code", terms: [], replacements: rules), "write C# source")
        XCTAssertEqual(VocabFixer.apply("pay $1", terms: [], replacements: [rule("pay", "$0 \\1")]), "$0 \\1 $1")
    }

    func testCustomWordsWithSymbolsAreMatchedWhenSaid() {
        XCTAssertEqual(VocabFixer.apply("Slash consolidate", terms: ["/consolidate"]), "/consolidate")
        XCTAssertEqual(
            VocabFixer.apply("then slash consolidate the notes", terms: ["/consolidate"]),
            "then /consolidate the notes"
        )
        XCTAssertEqual(VocabFixer.apply("I'll consolidate later", terms: ["/consolidate"]), "I'll consolidate later")
        XCTAssertEqual(VocabFixer.apply("built in c sharp", terms: ["C#"]), "built in C#")
        XCTAssertEqual(VocabFixer.apply("a node dot js app", terms: ["Node.js"]), "a Node.js app")
        XCTAssertEqual(VocabFixer.apply("the dotnet runtime", terms: [".NET"]), "the dotnet runtime")
    }

    func testWordsAndReplacementsTogether() {
        XCTAssertEqual(
            VocabFixer.apply("Tell Sophia omw.", terms: ["Sofia"], replacements: [rule("omw", "on my way")]),
            "Tell Sofia on my way."
        )
    }
}

final class SoundAlikeTests: XCTestCase {
    func testKeys() {
        XCTAssertEqual(SoundAlike.key("Sophia"), "SF")
        XCTAssertEqual(SoundAlike.key("Sofia"), "SF")
        XCTAssertEqual(SoundAlike.key("Better"), "BTR")
        XCTAssertEqual(SoundAlike.key("Bettor"), "BTR")
        XCTAssertEqual(SoundAlike.key("Shell"), "XL")
        XCTAssertEqual(SoundAlike.key("About"), "ABT")
        XCTAssertEqual(SoundAlike.key("Coppola"), SoundAlike.key("Copola"))
        XCTAssertEqual(SoundAlike.key("Knight"), SoundAlike.key("Night"))
        XCTAssertEqual(SoundAlike.key("Catherine"), SoundAlike.key("Kathryn"))
        XCTAssertEqual(SoundAlike.key("Geoff"), SoundAlike.key("Jeff"))
        XCTAssertEqual(SoundAlike.key("Zoë"), SoundAlike.key("Zoe"))
        XCTAssertNotEqual(SoundAlike.key("Shell"), SoundAlike.key("Sell"))
        XCTAssertEqual(SoundAlike.key("1234"), "")
        XCTAssertEqual(SoundAlike.key(""), "")
    }

    func testDistance() {
        XCTAssertEqual(SoundAlike.distance("sophia", "sofia"), 2)
        XCTAssertEqual(SoundAlike.distance("kitten", "sitting"), 3)
        XCTAssertEqual(SoundAlike.distance("", "abc"), 3)
        XCTAssertEqual(SoundAlike.distance("same", "same"), 0)
    }

    func testCommonWords() {
        XCTAssertTrue(SoundAlike.isCommonWord("Sofa"))
        XCTAssertTrue(SoundAlike.isCommonWord("the"))
        XCTAssertFalse(SoundAlike.isCommonWord("Sophia"))
        XCTAssertFalse(SoundAlike.commonWords.contains(""))
    }
}

final class SharedSettingsTests: XCTestCase {
    /// A throwaway defaults suite, removed when the test ends.
    private func makeSettings() -> SharedSettings {
        let suite = "shoutabout-tests-\(UUID().uuidString)"
        addTeardownBlock {
            UserDefaults.standard.removePersistentDomain(forName: suite)
        }
        return SharedSettings(defaults: UserDefaults(suiteName: suite) ?? .standard)
    }

    func testDefaults() {
        let settings = makeSettings()
        XCTAssertEqual(settings.target, .pc)
        XCTAssertTrue(settings.preferLAN)
        XCTAssertFalse(settings.forceOffline)
        XCTAssertTrue(settings.offlineFallback)
        XCTAssertEqual(settings.offlineEngine, .automatic)
        XCTAssertFalse(settings.configuration.isPaired)
    }

    func testRoundTrip() {
        let settings = makeSettings()
        settings.target = .phone
        settings.preferLAN = false
        settings.configuration = HubConfiguration(
            lanURL: URL(string: "http://192.168.1.50:8765"),
            publicURL: URL(string: "https://dictate.example.com"),
            homeSSID: "Home"
        )
        settings.vocabTerms = ["A", "B"]
        settings.vocabReplacements = [VocabReplacement(say: "omw", write: "on my way")]
        let again = SharedSettings(defaults: settings.defaults)
        XCTAssertEqual(again.target, .phone)
        XCTAssertFalse(again.preferLAN)
        XCTAssertEqual(again.configuration.lanURL?.absoluteString, "http://192.168.1.50:8765")
        XCTAssertEqual(again.configuration.homeSSID, "Home")
        XCTAssertEqual(again.vocabTerms, ["A", "B"])
        XCTAssertEqual(again.vocabReplacements, [VocabReplacement(say: "omw", write: "on my way")])
        again.vocabReplacements = []
        XCTAssertEqual(SharedSettings(defaults: settings.defaults).vocabReplacements, [])
    }

    func testPendingDictationIsConsumedOnceAndExpires() {
        let settings = makeSettings()
        let now = Date()
        XCTAssertFalse(PendingDictation.consume(in: settings, now: now))
        PendingDictation.request(in: settings, now: now)
        XCTAssertTrue(PendingDictation.consume(in: settings, now: now.addingTimeInterval(2)))
        XCTAssertFalse(PendingDictation.consume(in: settings, now: now.addingTimeInterval(3)))
        PendingDictation.request(in: settings, now: now)
        XCTAssertFalse(PendingDictation.consume(in: settings, now: now.addingTimeInterval(PendingDictation.maxAge + 5)))
    }

    func testTargetToggle() {
        XCTAssertEqual(DictationTarget.pc.toggled, .phone)
        XCTAssertEqual(DictationTarget.phone.toggled, .pc)
    }

    // MARK: Pending copy

    func testPendingCopyRoundTripsAndClears() {
        let settings = makeSettings()
        XCTAssertNil(settings.pendingCopy)
        let pending = PendingCopy(text: "Sample text: call Sofia.", createdAt: Date(timeIntervalSince1970: 1_000))
        settings.pendingCopy = pending
        XCTAssertEqual(SharedSettings(defaults: settings.defaults).pendingCopy, pending)
        settings.pendingCopy = nil
        XCTAssertNil(settings.pendingCopy)
    }

    func testPendingCopyIsOnlyFreshForAShortWhile() {
        let made = Date(timeIntervalSince1970: 10_000)
        let pending = PendingCopy(text: "x", createdAt: made)
        XCTAssertTrue(pending.isFresh(at: made))
        XCTAssertTrue(pending.isFresh(at: made.addingTimeInterval(PendingCopy.maxAge - 1)))
        XCTAssertFalse(pending.isFresh(at: made.addingTimeInterval(PendingCopy.maxAge + 1)))
        XCTAssertFalse(pending.isFresh(at: made.addingTimeInterval(-3600)))  // clock changed
    }
}

final class DeviceFeaturesTests: XCTestCase {
    func testActionButtonModels() {
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone16,1"), true)  // 15 Pro
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone16,2"), true)  // 15 Pro Max
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone17,5"), true)  // 16e
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone18,3"), true)
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone15,4"), false)  // 15
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone15,2"), false)  // 14 Pro
        XCTAssertEqual(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone14,6"), false)  // SE 3
        XCTAssertNil(DeviceFeatures.hasActionButton(modelIdentifier: "iPad14,1"))
        XCTAssertNil(DeviceFeatures.hasActionButton(modelIdentifier: "arm64"))
        XCTAssertNil(DeviceFeatures.hasActionButton(modelIdentifier: "iPhone"))
        XCTAssertNil(DeviceFeatures.hasActionButton(modelIdentifier: "iPhoneX,1"))
    }
}
