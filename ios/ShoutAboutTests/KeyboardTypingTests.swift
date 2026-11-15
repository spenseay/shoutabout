import XCTest

/// The ShoutAbout keyboard's layout and typing rules (Shared/Core/KeyboardTyping.swift).
final class KeyboardTypingTests: XCTestCase {
    private let width: CGFloat = 393  // iPhone 17 / 16 Pro
    private let top = KeyboardLayout.barHeight(compact: false)
    private var keysHeight: CGFloat { KeyboardLayout.height(compact: false) - top }

    private func frames(_ page: KeyboardPage = .letters, globe: Bool = false) -> [KeyboardKeyFrame] {
        KeyboardLayout.frames(page: page, showsGlobe: globe, width: width, top: top, height: keysHeight)
    }

    // MARK: Layout

    func testTheMouthSitsWhereTheEmojiKeyIs() {
        XCTAssertEqual(KeyboardLayout.rows(page: .letters, showsGlobe: false)[3], [.pageToggle, .mouth, .space, .returnKey])
        XCTAssertEqual(KeyboardLayout.rows(page: .letters, showsGlobe: true)[3], [.pageToggle, .globe, .mouth, .space, .returnKey])
        for page in [KeyboardPage.letters, .numbers, .symbols] {
            XCTAssertTrue(KeyboardLayout.rows(page: page, showsGlobe: false)[3].contains(.mouth))
        }
    }

    func testEveryPageHasTenKeysOnTop() {
        for page in [KeyboardPage.letters, .numbers, .symbols] {
            XCTAssertEqual(KeyboardLayout.rows(page: page, showsGlobe: false)[0].count, 10, "\(page)")
        }
        XCTAssertEqual(KeyboardLayout.rows(page: .numbers, showsGlobe: false)[1].count, 10)
        XCTAssertEqual(KeyboardLayout.rows(page: .symbols, showsGlobe: false)[1].count, 10)
    }

    func testKeysStayInsideTheKeyboardAndNeverOverlap() {
        for page in [KeyboardPage.letters, .numbers, .symbols] {
            for globe in [false, true] {
                let all = frames(page, globe: globe)
                for item in all {
                    XCTAssertGreaterThanOrEqual(item.frame.minX, 0, "\(item.key)")
                    XCTAssertLessThanOrEqual(item.frame.maxX, width + 0.01, "\(item.key)")
                    XCTAssertGreaterThanOrEqual(item.frame.minY, top)
                    XCTAssertGreaterThan(item.frame.width, 20, "\(item.key)")
                }
                for (i, a) in all.enumerated() {
                    for b in all[(i + 1)...] {
                        XCTAssertFalse(a.frame.insetBy(dx: 0.5, dy: 0.5).intersects(b.frame), "\(a.key) overlaps \(b.key)")
                    }
                }
            }
        }
    }

    func testEveryPointOnTheKeysBelongsToAKey() {
        let all = frames()
        var y = top + 1
        while y < top + keysHeight {
            var x: CGFloat = 0.5
            while x < width {
                XCTAssertNotNil(KeyboardLayout.key(at: CGPoint(x: x, y: y), in: all), "no key at \(x), \(y)")
                x += 3
            }
            y += 3
        }
        XCTAssertNil(KeyboardLayout.key(at: CGPoint(x: 100, y: top - 5), in: all), "the suggestion bar isn't a key")
    }

    func testATouchInTheGapGoesToTheNearestKey() {
        let all = frames()
        guard let q = all.first(where: { $0.key == .character("q") }),
              let w = all.first(where: { $0.key == .character("w") }) else {
            return XCTFail("no q or w")
        }
        let gapX = (q.frame.maxX + w.frame.minX) / 2
        XCTAssertEqual(KeyboardLayout.key(at: CGPoint(x: gapX - 0.5, y: q.frame.midY), in: all), .character("q"))
        XCTAssertEqual(KeyboardLayout.key(at: CGPoint(x: gapX + 0.5, y: q.frame.midY), in: all), .character("w"))
        XCTAssertEqual(KeyboardLayout.key(at: CGPoint(x: 1, y: q.frame.midY), in: all), .character("q"))
    }

    func testSpaceIsTheWidestKeyAndTheMouthIsAProperKey() {
        let all = frames()
        guard let space = all.first(where: { $0.key == .space }),
              let mouth = all.first(where: { $0.key == .mouth }),
              let a = all.first(where: { $0.key == .character("a") }) else {
            return XCTFail("missing keys")
        }
        XCTAssertEqual(all.map(\.frame.width).max(), space.frame.width)
        XCTAssertGreaterThan(mouth.frame.width, a.frame.width)
        XCTAssertEqual(mouth.frame.height, a.frame.height)
    }

    // MARK: Capitals

    func testSentencesStartWithACapital() {
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: nil))
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: ""))
        XCTAssertFalse(KeyboardTyping.capitalizes(.sentences, before: "Hello"))
        XCTAssertFalse(KeyboardTyping.capitalizes(.sentences, before: "Hello "))
        XCTAssertFalse(KeyboardTyping.capitalizes(.sentences, before: "Hello."))
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: "Hello. "))
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: "Really?  "))
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: "Hi\n"))
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: "He said \"Hi.\" "))
        XCTAssertTrue(KeyboardTyping.capitalizes(.sentences, before: "   "))
    }

    func testOtherCapitalSettings() {
        XCTAssertTrue(KeyboardTyping.capitalizes(.words, before: "hello "))
        XCTAssertFalse(KeyboardTyping.capitalizes(.words, before: "hello"))
        XCTAssertFalse(KeyboardTyping.capitalizes(.none, before: ""))
        XCTAssertTrue(KeyboardTyping.capitalizes(.allCharacters, before: "ABC"))
    }

    // MARK: Shift

    func testShiftTapsAndCapsLock() {
        let t = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertEqual(KeyboardTyping.tapShift(.off, lastTapAt: nil, now: t), .on)
        XCTAssertEqual(KeyboardTyping.tapShift(.on, lastTapAt: nil, now: t), .off)
        XCTAssertEqual(KeyboardTyping.tapShift(.on, lastTapAt: t, now: t.addingTimeInterval(0.2)), .locked)
        // A capital the keyboard turned on, tapped off and quickly on again: caps lock too.
        XCTAssertEqual(KeyboardTyping.tapShift(.off, lastTapAt: t, now: t.addingTimeInterval(0.2)), .locked)
        XCTAssertEqual(KeyboardTyping.tapShift(.on, lastTapAt: t, now: t.addingTimeInterval(1)), .off)
        XCTAssertEqual(KeyboardTyping.tapShift(.locked, lastTapAt: t, now: t.addingTimeInterval(0.1)), .off)
        XCTAssertEqual(KeyboardTyping.shiftAfterCharacter(.on), .off)
        XCTAssertEqual(KeyboardTyping.shiftAfterCharacter(.locked), .locked)
        XCTAssertEqual(KeyboardTyping.shiftAfterCharacter(.off), .off)
    }

    // MARK: Spaces and words

    func testTwoSpacesMakeAPeriodOnlyAfterAWord() {
        XCTAssertTrue(KeyboardTyping.doubleSpaceMakesPeriod(before: "Hello "))
        XCTAssertTrue(KeyboardTyping.doubleSpaceMakesPeriod(before: "at 5 "))
        XCTAssertFalse(KeyboardTyping.doubleSpaceMakesPeriod(before: "Hello  "))
        XCTAssertFalse(KeyboardTyping.doubleSpaceMakesPeriod(before: "Hello. "))
        XCTAssertFalse(KeyboardTyping.doubleSpaceMakesPeriod(before: "Hello"))
        XCTAssertFalse(KeyboardTyping.doubleSpaceMakesPeriod(before: " "))
        XCTAssertFalse(KeyboardTyping.doubleSpaceMakesPeriod(before: nil))
    }

    func testTheWordBeingTyped() {
        XCTAssertEqual(KeyboardTyping.currentWord(before: "I love teh"), "teh")
        XCTAssertEqual(KeyboardTyping.currentWord(before: "teh "), "")
        XCTAssertEqual(KeyboardTyping.currentWord(before: "I don't"), "don't")
        XCTAssertEqual(KeyboardTyping.currentWord(before: "mail me@exam"), "")
        XCTAssertEqual(KeyboardTyping.currentWord(before: "www.goo"), "")
        XCTAssertEqual(KeyboardTyping.currentWord(before: "at 3pm"), "")
        XCTAssertEqual(KeyboardTyping.currentWord(before: nil), "")
    }

    func testHoldingDeleteRemovesWholeWords() {
        XCTAssertEqual(KeyboardTyping.wordDeletionLength(before: "hello world"), 5)
        XCTAssertEqual(KeyboardTyping.wordDeletionLength(before: "hello world  "), 7)
        XCTAssertEqual(KeyboardTyping.wordDeletionLength(before: ""), 1)
        XCTAssertEqual(KeyboardTyping.wordDeletionLength(before: nil), 1)
    }

    // MARK: Corrections

    func testCommonTyposAreCorrected() {
        XCTAssertEqual(KeyboardTyping.correction(for: "teh", misspelled: true, guesses: ["the", "ten"], replacement: nil), "the")
        XCTAssertEqual(KeyboardTyping.correction(for: "Teh", misspelled: true, guesses: ["the"], replacement: nil), "The")
        XCTAssertEqual(KeyboardTyping.correction(for: "recieve", misspelled: true, guesses: ["receive"], replacement: nil), "receive")
        XCTAssertEqual(KeyboardTyping.correction(for: "tommorow", misspelled: true, guesses: ["tomorrow"], replacement: nil), "tomorrow")
    }

    func testApostrophesAndTheLoneI() {
        XCTAssertEqual(KeyboardTyping.correction(for: "i", misspelled: false, guesses: [], replacement: nil), "I")
        XCTAssertEqual(KeyboardTyping.correction(for: "im", misspelled: true, guesses: [], replacement: nil), "I'm")
        XCTAssertEqual(KeyboardTyping.correction(for: "dont", misspelled: true, guesses: [], replacement: nil), "don't")
        XCTAssertEqual(KeyboardTyping.correction(for: "Dont", misspelled: true, guesses: [], replacement: nil), "Don't")
        XCTAssertNil(KeyboardTyping.correction(for: "I", misspelled: false, guesses: [], replacement: nil))
        // Real words that only look like missing apostrophes stay.
        XCTAssertNil(KeyboardTyping.correction(for: "ill", misspelled: false, guesses: [], replacement: nil))
        XCTAssertNil(KeyboardTyping.correction(for: "lets", misspelled: false, guesses: [], replacement: nil))
    }

    func testCorrectionsAreCareful() {
        // Too far from the guess, too short, in capitals, or a contact's name: left alone.
        XCTAssertNil(KeyboardTyping.correction(for: "xyzzy", misspelled: true, guesses: ["fuzzy"], replacement: nil))
        XCTAssertNil(KeyboardTyping.correction(for: "ot", misspelled: true, guesses: ["to"], replacement: nil))
        XCTAssertNil(KeyboardTyping.correction(for: "ASDF", misspelled: true, guesses: ["ASD"], replacement: nil))
        XCTAssertNil(KeyboardTyping.correction(for: "Sofia", misspelled: true, guesses: ["Sofa"], replacement: nil, known: ["sofia"]))
        XCTAssertNil(KeyboardTyping.correction(for: "hello", misspelled: false, guesses: [], replacement: nil))
        XCTAssertNil(KeyboardTyping.correction(for: "", misspelled: true, guesses: ["a"], replacement: nil))
    }

    func testTextReplacementsFromSettingsWin() {
        XCTAssertEqual(KeyboardTyping.correction(for: "omw", misspelled: true, guesses: ["mow"], replacement: "On my way!"), "On my way!")
    }

    func testTheSuggestionBar() {
        let bar = KeyboardTyping.suggestions(typed: "teh", correction: "the", guesses: ["the", "ten", "tea"], completions: [])
        XCTAssertEqual(bar.map(\.text), ["teh", "the", "ten"])
        XCTAssertEqual(bar.map(\.kind), [.typed, .correction, .alternative])

        let longer = KeyboardTyping.suggestions(typed: "Hel", correction: nil, guesses: [], completions: ["hello", "help", "helmet"])
        XCTAssertEqual(longer.map(\.text), ["Hel", "Hello", "Help"])

        XCTAssertEqual(KeyboardTyping.suggestions(typed: "", correction: nil, guesses: ["a"], completions: []), [])
    }

    func testEditDistance() {
        XCTAssertEqual(KeyboardTyping.editDistance("teh", "the"), 1)
        XCTAssertEqual(KeyboardTyping.editDistance("kitten", "sitting"), 3)
        XCTAssertEqual(KeyboardTyping.editDistance("", "abc"), 3)
        XCTAssertEqual(KeyboardTyping.editDistance("same", "same"), 0)
    }

    // MARK: Undo

    func testUndoOnlyWhileTheDictationIsRightBeforeTheCursor() {
        XCTAssertTrue(KeyboardTyping.isJustBeforeCursor(" see you at six.", before: "Sounds good, see you at six."))
        XCTAssertFalse(KeyboardTyping.isJustBeforeCursor(" see you at six.", before: "Sounds good, see you at six. Bring"))
        XCTAssertFalse(KeyboardTyping.isJustBeforeCursor("six", before: nil))
        XCTAssertFalse(KeyboardTyping.isJustBeforeCursor("", before: "six"))
        // iOS showed only the end of a long dictation: long enough to be sure.
        let long = "Running about ten minutes late, save me a seat by the window please."
        XCTAssertTrue(KeyboardTyping.isJustBeforeCursor(long, before: String(long.suffix(30))))
        XCTAssertFalse(KeyboardTyping.isJustBeforeCursor(long, before: "please."))
    }

    // MARK: Up and down on the space bar

    /// Every character 10 points wide.
    private func lines(_ text: String, width: CGFloat) -> KeyboardTextLines {
        KeyboardTextLines(Array(text), width: width) { _ in 10 }
    }

    func testLinesBreakAtLineBreaksAndWrapWholeWords() {
        XCTAssertEqual(lines("ab\ncd", width: 1000).lineStarts, [0, 3])
        // "hello " fits 60 points (the space hangs past the edge), "world" goes down.
        XCTAssertEqual(lines("hello world", width: 60).lineStarts, [0, 6])
        // One word wider than the line is broken where it runs out of room.
        XCTAssertEqual(lines("abcdefghij", width: 40).lineStarts, [0, 4, 8])
        // A line break at the very end: an empty last line for the cursor.
        XCTAssertEqual(lines("ab\n", width: 1000).lineStarts, [0, 3])
        XCTAssertEqual(lines("", width: 100).lineStarts, [0])
        XCTAssertEqual(lines("one two three four", width: 90).lineStarts, [0, 8, 14])
    }

    func testWhereTheCursorIs() {
        let text = lines("hello world", width: 60)
        XCTAssertEqual(text.line(of: 0), 0)
        XCTAssertEqual(text.line(of: 5), 0)
        XCTAssertEqual(text.line(of: 6), 1)  // where it wraps: the line it starts
        XCTAssertEqual(text.line(of: 11), 1)
        XCTAssertEqual(text.x(of: 8), 20)
        XCTAssertEqual(text.x(of: 3), 30)
    }

    func testUpAndDownKeepTheirPlaceAcrossAShortLine() {
        let text = lines("a long line\nab\nanother long", width: 1000)
        XCTAssertEqual(text.lineStarts, [0, 12, 15])
        // From "a long li|ne" (90 points along): the short line's end, then back to 90 points.
        let goal = text.x(of: 9)
        XCTAssertEqual(goal, 90)
        XCTAssertEqual(text.position(onLine: 1, nearestX: goal), 14)  // before its line break
        XCTAssertEqual(text.position(onLine: 2, nearestX: goal), 24)
        XCTAssertEqual(text.position(onLine: 0, nearestX: 0), 0)
        XCTAssertEqual(text.position(onLine: 2, nearestX: 1000), 27)  // the end of the text
        // Lines past either end are clamped.
        XCTAssertEqual(text.position(onLine: -3, nearestX: 30), 3)
        XCTAssertEqual(text.position(onLine: 9, nearestX: 30), 18)
    }

    func testAWrappedLineEndsBeforeTheSpaceItWrapsAt() {
        let text = lines("hello world", width: 60)
        XCTAssertEqual(text.position(onLine: 0, nearestX: 1000), 5)
        XCTAssertEqual(text.position(onLine: 1, nearestX: 1000), 11)
        XCTAssertEqual(text.position(onLine: 1, nearestX: 24), 8)  // nearest: 20 beats 30
    }

    func testCursorMovesCountUTF16() {
        let text = lines("a\u{1F600}b", width: 1000)
        XCTAssertEqual(text.characterCount, 3)
        XCTAssertEqual(text.utf16Distance(from: 1, to: 2), 2)  // over the emoji
        XCTAssertEqual(text.utf16Distance(from: 3, to: 0), -4)
        XCTAssertEqual(text.utf16Distance(from: 2, to: 2), 0)
    }
}
