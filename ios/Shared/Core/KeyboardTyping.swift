import CoreGraphics
import Foundation

// The ShoutAbout keyboard's typing: an iPhone-style layout with the mouth where the emoji key
// sits, and the small rules that make typing feel like the iPhone keyboard (a capital at the
// start of a sentence, a period for two spaces, shift and caps lock, suggestions and a careful
// autocorrect). Pure functions, unit-tested in ShoutAboutTests; the keyboard only acts on them.

/// Which keys show.
enum KeyboardPage: Equatable, Sendable {
    case letters
    case numbers
    case symbols
}

enum KeyboardShift: Equatable, Sendable {
    case off
    /// The next letter only (tapped, or a new sentence).
    case on
    /// Caps lock (shift tapped twice quickly).
    case locked
}

enum KeyboardKey: Hashable, Sendable {
    /// What it types: lower case on the letters page (shift makes it upper case).
    case character(String)
    case shift
    case delete
    /// "123" on the letters page, "ABC" on the others.
    case pageToggle
    /// "#+=" on the numbers page, "123" on the symbols page.
    case symbolsToggle
    /// iOS's keyboard switcher, only when iOS asks for one (`needsInputModeSwitchKey`).
    case globe
    /// Dictate (where the iPhone keyboard has its emoji key).
    case mouth
    case space
    case returnKey
}

struct KeyboardKeyFrame: Equatable, Sendable {
    var key: KeyboardKey
    /// Where the key is drawn.
    var frame: CGRect
    /// Where a touch counts for it: the key plus half the gap around it, so there are no dead
    /// spots between keys (as on the iPhone keyboard).
    var hitFrame: CGRect
}

enum KeyboardLayout {
    static let sideMargin: CGFloat = 3
    static let keyGap: CGFloat = 6
    /// The suggestion bar above the keys.
    static func barHeight(compact: Bool) -> CGFloat { compact ? 36 : 44 }
    /// The whole keyboard: the bar and four rows of keys.
    static func height(compact: Bool) -> CGFloat { compact ? 198 : 262 }
    /// Cancel on the listening panel (the panel fills the keyboard).
    static let cancelFrame = CGRect(x: 14, y: 14, width: 112, height: 40)

    static func rows(page: KeyboardPage, showsGlobe: Bool) -> [[KeyboardKey]] {
        func chars(_ s: String) -> [KeyboardKey] { s.map { .character(String($0)) } }
        let bottom: [KeyboardKey] = showsGlobe
            ? [.pageToggle, .globe, .mouth, .space, .returnKey]
            : [.pageToggle, .mouth, .space, .returnKey]
        let punctuation: [KeyboardKey] = [KeyboardKey.symbolsToggle] + chars(".,?!'") + [KeyboardKey.delete]
        switch page {
        case .letters:
            let third: [KeyboardKey] = [KeyboardKey.shift] + chars("zxcvbnm") + [KeyboardKey.delete]
            return [chars("qwertyuiop"), chars("asdfghjkl"), third, bottom]
        case .numbers:
            return [chars("1234567890"), chars("-/:;()$&@\""), punctuation, bottom]
        case .symbols:
            return [chars("[]{}#%^*+="), chars("_\\|~<>€£¥•"), punctuation, bottom]
        }
    }

    /// The keys' frames for a keyboard `width` wide whose keys fill `top ..< top + height`.
    static func frames(page: KeyboardPage, showsGlobe: Bool, width: CGFloat, top: CGFloat, height: CGFloat) -> [KeyboardKeyFrame] {
        guard width > 40, height > 40 else { return [] }
        let rows = rows(page: page, showsGlobe: showsGlobe)
        let pitch = height / CGFloat(rows.count)
        let keyHeight = (pitch * 0.8).rounded()
        let unit = (width - 2 * sideMargin - 9 * keyGap) / 10
        let wide = (unit * 1.36).rounded()

        var result: [KeyboardKeyFrame] = []
        for (index, row) in rows.enumerated() {
            let y = top + CGFloat(index) * pitch + (pitch - keyHeight) / 2
            var spans: [(KeyboardKey, CGFloat, CGFloat)] = []  // key, x, width

            func centered(_ keys: [KeyboardKey], keyWidth: CGFloat) {
                let block = CGFloat(keys.count) * keyWidth + CGFloat(keys.count - 1) * keyGap
                var x = (width - block) / 2
                for key in keys {
                    spans.append((key, x, keyWidth))
                    x += keyWidth + keyGap
                }
            }

            switch index {
            case 0, 1:
                centered(row, keyWidth: unit)
            case 2:
                // A wide key at each end (shift or #+=, and delete), the rest in between.
                let middle = Array(row.dropFirst().dropLast())
                spans.append((row[0], sideMargin, wide))
                if page == .letters {
                    centered(middle, keyWidth: unit)
                } else {
                    let room = width - 2 * sideMargin - 2 * wide - 2 * keyGap * 2
                    let keyWidth = (room - CGFloat(middle.count - 1) * keyGap) / CGFloat(middle.count)
                    var x = sideMargin + wide + keyGap * 2
                    for key in middle {
                        spans.append((key, x, keyWidth))
                        x += keyWidth + keyGap
                    }
                }
                spans.append((row[row.count - 1], width - sideMargin - wide, wide))
            default:
                // 123, (globe,) mouth, space, return: space takes what's left.
                let small = (unit * 1.25).rounded()
                let ret = (unit * 2.45).rounded()
                let fixed = small * CGFloat(row.count - 2) + ret
                let space = width - 2 * sideMargin - fixed - CGFloat(row.count - 1) * keyGap
                var x = sideMargin
                for key in row {
                    let w: CGFloat
                    switch key {
                    case .space: w = space
                    case .returnKey: w = ret
                    default: w = small
                    }
                    spans.append((key, x, w))
                    x += w + keyGap
                }
            }

            // Touch areas: halfway into each gap, out to the edges on the ends and the outer rows.
            let bandTop = index == 0 ? top : top + CGFloat(index) * pitch
            let bandBottom = index == rows.count - 1 ? top + height : top + CGFloat(index + 1) * pitch
            for (position, span) in spans.enumerated() {
                let (key, x, w) = span
                let left = position == 0 ? 0 : (spans[position - 1].1 + spans[position - 1].2 + x) / 2
                let right = position == spans.count - 1 ? width : (x + w + spans[position + 1].1) / 2
                result.append(KeyboardKeyFrame(
                    key: key,
                    frame: CGRect(x: x, y: y, width: w, height: keyHeight),
                    hitFrame: CGRect(x: left, y: bandTop, width: right - left, height: bandBottom - bandTop)
                ))
            }
        }
        return result
    }

    /// The key a touch at `point` is for (nil above the keys).
    static func key(at point: CGPoint, in frames: [KeyboardKeyFrame]) -> KeyboardKey? {
        frames.first { $0.hitFrame.contains(point) }?.key
    }
}

/// The text field's capitalization setting (UITextAutocapitalizationType).
enum KeyboardAutoCapitalization: Sendable {
    case none
    case words
    case sentences
    case allCharacters
}

/// A word in the suggestion bar.
struct KeyboardSuggestion: Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        /// What was typed, kept as is (tapping it also teaches the spell checker the word).
        case typed
        /// What space would change the word to.
        case correction
        /// Another spelling or a longer word.
        case alternative
    }

    var text: String
    var kind: Kind
}

enum KeyboardTyping {
    /// Two taps on shift within this make caps lock.
    static let doubleTapWindow: TimeInterval = 0.35
    /// A second space within this after the first types ". " instead.
    static let doubleSpaceWindow: TimeInterval = 2

    /// Should the next letter be a capital?
    static func capitalizes(_ mode: KeyboardAutoCapitalization, before: String?) -> Bool {
        let text = before ?? ""
        switch mode {
        case .none:
            return false
        case .allCharacters:
            return true
        case .words:
            guard let last = text.last else { return true }
            return last.isWhitespace
        case .sentences:
            guard let last = text.last else { return true }
            if last.isNewline { return true }
            guard last == " " else { return false }
            // The last character before the spaces, looking through closing quotes and brackets
            // ('He said "Hi." ' starts a new sentence).
            guard let end = text.reversed().first(where: { $0 != " " && !"\"”’')".contains($0) }) else {
                return true  // only spaces so far
            }
            return end.isNewline || ".!?".contains(end)
        }
    }

    /// Shift after a tap on it: on, off, or caps lock for a quick second tap.
    static func tapShift(_ shift: KeyboardShift, lastTapAt: Date?, now: Date) -> KeyboardShift {
        if shift == .locked { return .off }
        if let lastTapAt, now.timeIntervalSince(lastTapAt) < doubleTapWindow { return .locked }
        return shift == .off ? .on : .off
    }

    /// Shift after a letter: a one-letter shift is used up; caps lock stays.
    static func shiftAfterCharacter(_ shift: KeyboardShift) -> KeyboardShift {
        shift == .on ? .off : shift
    }

    /// Two spaces after a word make ". " (`before` already ends with the first space).
    static func doubleSpaceMakesPeriod(before: String?) -> Bool {
        guard let text = before, text.hasSuffix(" ") else { return false }
        let rest = text.dropLast()
        guard let last = rest.last else { return false }
        return last.isLetter || last.isNumber || ")\"'”’]".contains(last)
    }

    /// The word being typed: the letters (and apostrophes) just before the cursor. Empty when
    /// the cursor follows a space or punctuation, or the "word" is part of an address, a
    /// number or a handle (nothing to spell-check there).
    static func currentWord(before: String?) -> String {
        guard let text = before else { return "" }
        let word = String(text.reversed().prefix { $0.isLetter || $0 == "'" || $0 == "’" }.reversed())
        guard !word.isEmpty else { return "" }
        let preceding = text.dropLast(word.count).last
        if let preceding, preceding.isNumber || "@/.:_#\\-".contains(preceding) { return "" }
        return word
    }

    /// How many characters a long press on delete removes at a time once it speeds up: back to
    /// the start of the word before the cursor (with the spaces after it).
    static func wordDeletionLength(before: String?) -> Int {
        guard let text = before, !text.isEmpty else { return 1 }
        let spaces = text.reversed().prefix { $0.isWhitespace }.count
        let word = text.dropLast(spaces).reversed().prefix { !$0.isWhitespace }.count
        return max(1, spaces + word)
    }

    /// `word` in the same case as `typed` ("Teh" -> "The", "TEH" -> "THE").
    static func matchingCase(of typed: String, _ word: String) -> String {
        guard let first = typed.first, first.isUppercase else { return word }
        if typed.count > 1, typed == typed.uppercased() { return word.uppercased() }
        return word.prefix(1).uppercased() + word.dropFirst()
    }

    /// Missing apostrophes and the lone "i": fixed even though the spell checker may not flag
    /// them ("cant" and "wont" are words, but rarely meant).
    static let contractions: [String: String] = [
        "i": "I", "im": "I'm", "ive": "I've", "ill": "I'll", "id": "I'd",
        "dont": "don't", "cant": "can't", "wont": "won't", "didnt": "didn't", "doesnt": "doesn't",
        "isnt": "isn't", "wasnt": "wasn't", "arent": "aren't", "werent": "weren't",
        "couldnt": "couldn't", "shouldnt": "shouldn't", "wouldnt": "wouldn't", "havent": "haven't",
        "hasnt": "hasn't", "thats": "that's", "whats": "what's", "theyre": "they're",
        "youre": "you're", "youll": "you'll", "youve": "you've", "theyll": "they'll",
        "theyve": "they've", "weve": "we've", "lets": "let's", "hes": "he's", "shes": "she's",
    ]

    /// Words a correction never touches even when the spell checker suggests one ("ill", "id",
    /// "lets", "wont" and "cant" are real words too; only these few are left alone).
    private static let keepAsTyped: Set<String> = ["ill", "id", "lets", "wont", "hes", "shes"]

    /// What space changes the word to, or nil to leave it. Careful on purpose: a text
    /// replacement from Settings, a missing apostrophe, or a misspelling whose best guess is one
    /// or two letters away (two only for longer words). Never names the user's contacts or
    /// words in capitals.
    static func correction(
        for typed: String,
        misspelled: Bool,
        guesses: [String],
        replacement: String?,
        known: Set<String> = []
    ) -> String? {
        guard !typed.isEmpty else { return nil }
        if let replacement, replacement != typed { return replacement }
        let lower = typed.lowercased()
        if known.contains(lower) { return nil }
        if let fixed = contractions[lower], !keepAsTyped.contains(lower) {
            // "I" stays a capital whatever was typed.
            let result = fixed.hasPrefix("I") ? fixed : matchingCase(of: typed, fixed)
            return result == typed ? nil : result
        }
        guard misspelled, typed.count >= 3 else { return nil }
        if typed.count > 1, typed == typed.uppercased() { return nil }  // an acronym
        guard let best = guesses.first else { return nil }
        let distance = editDistance(lower, best.lowercased())
        guard distance >= 1, distance <= (typed.count >= 6 ? 2 : 1) else { return nil }
        return matchingCase(of: typed, best)
    }

    /// The suggestion bar for `typed`: what was typed, then the correction (if any), then
    /// another guess or a longer word. At most three, no repeats.
    static func suggestions(typed: String, correction: String?, guesses: [String], completions: [String]) -> [KeyboardSuggestion] {
        guard !typed.isEmpty else { return [] }
        var items = [KeyboardSuggestion(text: typed, kind: .typed)]
        var seen: Set<String> = [typed.lowercased()]
        if let correction {
            items.append(KeyboardSuggestion(text: correction, kind: .correction))
            seen.insert(correction.lowercased())
        }
        for word in guesses + completions where items.count < 3 {
            let shown = matchingCase(of: typed, word)
            guard seen.insert(shown.lowercased()).inserted else { continue }
            items.append(KeyboardSuggestion(text: shown, kind: .alternative))
        }
        return items
    }

    /// Is `typed` (a dictation the keyboard typed) right before the cursor, so Undo can take it
    /// back? iOS may show a keyboard only the end of a long text, so a `before` that's all the
    /// end of `typed` counts too, when it's long enough to be sure.
    static func isJustBeforeCursor(_ typed: String, before: String?) -> Bool {
        guard let before, !before.isEmpty, !typed.isEmpty else { return false }
        if before.hasSuffix(typed) { return true }
        return before.count >= 24 && typed.hasSuffix(before)
    }

    /// Edits (insert, delete, change, swap two neighbours) from `a` to `b`.
    static func editDistance(_ a: String, _ b: String) -> Int {
        let s = Array(a), t = Array(b)
        if s.isEmpty { return t.count }
        if t.isEmpty { return s.count }
        var d = Array(repeating: Array(repeating: 0, count: t.count + 1), count: s.count + 1)
        for i in 0...s.count { d[i][0] = i }
        for j in 0...t.count { d[0][j] = j }
        for i in 1...s.count {
            for j in 1...t.count {
                let cost = s[i - 1] == t[j - 1] ? 0 : 1
                d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
                if i > 1, j > 1, s[i - 1] == t[j - 2], s[i - 2] == t[j - 1] {
                    d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
                }
            }
        }
        return d[s.count][t.count]
    }
}

/// The text around the cursor laid out in lines, so dragging on the space bar can move the
/// cursor up and down as well as along. iOS tells a keyboard nothing about where a text field's
/// lines break, so this lays the text out the way the field probably does: a new line after
/// each line break in the text, and words wrapped to fit `width` points (`characterWidth`
/// measures in the field's likely font). Exact across the line breaks you typed; an estimate
/// inside a long wrapped paragraph. Positions count the text's `Character`s (0 ... count).
struct KeyboardTextLines {
    let characters: [Character]
    /// Each line's first position; a line runs to the next one's start.
    let lineStarts: [Int]
    private let widths: [CGFloat]
    /// UTF-16 length up to each position: text fields count cursor moves in UTF-16 units.
    private let utf16Offsets: [Int]

    init(_ characters: [Character], width: CGFloat, characterWidth: (Character) -> CGFloat) {
        self.characters = characters
        let widths = characters.map(characterWidth)
        self.widths = widths
        var offsets = [0]
        offsets.reserveCapacity(characters.count + 1)
        for character in characters {
            offsets.append(offsets[offsets.count - 1] + character.utf16.count)
        }
        utf16Offsets = offsets
        lineStarts = Self.wrap(characters, widths: widths, width: max(width, 1))
    }

    var characterCount: Int { characters.count }
    var lineCount: Int { lineStarts.count }

    /// The line the cursor at `position` is on (where a line wraps: the line it starts).
    func line(of position: Int) -> Int {
        var low = 0
        var high = lineStarts.count - 1
        while low < high {
            let middle = (low + high + 1) / 2
            if lineStarts[middle] <= position {
                low = middle
            } else {
                high = middle - 1
            }
        }
        return low
    }

    /// How far along its line the cursor at `position` is, in points.
    func x(of position: Int) -> CGFloat {
        let start = lineStarts[line(of: position)]
        let end = min(max(position, start), widths.count)
        return widths[start..<end].reduce(0, +)
    }

    /// The cursor position on `line` nearest to `x` points along it (the end of a shorter
    /// line, before its line break or the space it wraps at).
    func position(onLine line: Int, nearestX x: CGFloat) -> Int {
        let line = min(max(line, 0), lineStarts.count - 1)
        let start = lineStarts[line]
        let last = lastPosition(onLine: line)
        var best = start
        var bestDistance = abs(x)
        var along: CGFloat = 0
        var position = start
        while position < last {
            along += widths[position]
            position += 1
            let distance = abs(along - x)
            if distance < bestDistance {
                best = position
                bestDistance = distance
            }
        }
        return best
    }

    /// UTF-16 units from one position to another (negative going back), for
    /// `adjustTextPosition(byCharacterOffset:)`.
    func utf16Distance(from: Int, to: Int) -> Int {
        utf16Offsets[to] - utf16Offsets[from]
    }

    private func lastPosition(onLine line: Int) -> Int {
        guard line + 1 < lineStarts.count else { return characters.count }
        return max(lineStarts[line], lineStarts[line + 1] - 1)
    }

    /// Greedy word wrap: spaces hang past the edge, a word that doesn't fit starts the next
    /// line, and one word wider than the line is broken where it runs out of room.
    private static func wrap(_ characters: [Character], widths: [CGFloat], width: CGFloat) -> [Int] {
        var starts = [0]
        var lineStart = 0
        var lineWidth: CGFloat = 0
        var breakAt: Int?  // where this line could wrap: the start of the word after spaces
        var widthBeforeBreak: CGFloat = 0
        var index = 0
        while index < characters.count {
            let character = characters[index]
            if character.isNewline {
                index += 1
                starts.append(index)
                lineStart = index
                lineWidth = 0
                breakAt = nil
                continue
            }
            if character.isWhitespace {
                lineWidth += widths[index]
                index += 1
                if index < characters.count, !characters[index].isWhitespace {
                    breakAt = index
                    widthBeforeBreak = lineWidth
                }
                continue
            }
            if lineWidth + widths[index] > width, index > lineStart {
                if let at = breakAt, at > lineStart {
                    starts.append(at)
                    lineWidth -= widthBeforeBreak
                    lineStart = at
                } else {
                    starts.append(index)
                    lineWidth = 0
                    lineStart = index
                }
                breakAt = nil
                continue  // this character again, on the new line
            }
            lineWidth += widths[index]
            index += 1
        }
        return starts
    }
}
