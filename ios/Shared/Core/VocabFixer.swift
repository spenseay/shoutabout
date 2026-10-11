import Foundation

/// Post-processing for on-device transcripts, with the PC's custom words. The PC route doesn't
/// need this: the hub feeds the vocabulary to Whisper and its cleanup LLM.
///
/// Two passes:
///
/// 1. **Same spelling**: case-insensitive matches of a term become its canonical spelling
///    ("better shell" -> "Better Shell", "shout-about" -> "ShoutAbout").
/// 2. **Sound-alike**: a run of words that *sounds like* a term and is spelled almost like it
///    becomes the term ("Sophia" -> "Sofia", "Shout Aboot" -> "ShoutAbout"). The recognizer
///    spells a name the way it knows it, so this is what makes a custom spelling stick.
///
/// The sound-alike rule (built to leave ordinary sentences alone):
/// - **Shape**: the same number of words as the term (camelCase counts as words, so
///   "ShoutAbout" also matches two spoken words), only spaces or hyphens between them, and
///   about the same length (letters within 30%, at least 2).
/// - **Each word** is either spelled like the term's word (an "anchor") or sounds like it:
///   both have 3+ letters (shorter words and anything with digits must match exactly), the
///   same phonetic key (`SoundAlike.key`, a simplified Metaphone), and at most 2 edits apart
///   and no more than 40% of the longer word. Terms with fewer than 4 letters are never
///   matched by sound.
/// - **Evidence** that the speaker meant the term, for every sound-alike word: the recognizer
///   capitalised it (it heard a name: "Sophia"), or another word of the term matched exactly
///   and neither word is common English ("bettor shell" -> "Better Shell").
/// - **Common English words** ("sofa", "shoot", "simple", "cloud") are only rewritten into a
///   name or brand (a term with a capital letter) and only when the recognizer capitalised
///   them in mid-sentence, so "I sat on the sofa" never becomes "Sofia" and "shoot about ten
///   photos" never becomes "ShoutAbout".
/// - **Longest term wins** ("Sofia Coppola" before "Sofia"), words already spelled exactly
///   like a term are never rewritten into another term, and each word is rewritten once.
enum VocabFixer {
    static func apply(_ text: String, terms: [String]) -> String {
        guard !text.isEmpty else { return text }
        let cleaned = terms
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !cleaned.isEmpty else { return text }
        return fixSoundAlikes(fixSpelling(text, terms: cleaned), terms: cleaned)
    }

    // MARK: Pass 1: same spelling

    static func fixSpelling(_ text: String, terms: [String]) -> String {
        // Longest first so "Better Shell Pro" wins over "Better Shell".
        let ordered = terms.sorted { $0.count > $1.count }
        var result = text
        for term in ordered {
            guard let regex = pattern(for: term) else { continue }
            let range = NSRange(result.startIndex..., in: result)
            let template = NSRegularExpression.escapedTemplate(for: term)
            result = regex.stringByReplacingMatches(in: result, range: range, withTemplate: template)
        }
        return result
    }

    /// Words of the term (camelCase split too, so "ShoutAbout" matches "shout about")
    /// separated by optional spaces/hyphens, bounded by non-alphanumerics.
    static func pattern(for term: String) -> NSRegularExpression? {
        let words = term
            .split(whereSeparator: { $0.isWhitespace || $0 == "-" })
            .flatMap { splitCamelCase(String($0)) }
        guard !words.isEmpty else { return nil }
        let body = words.map(NSRegularExpression.escapedPattern(for:)).joined(separator: "[\\s-]*")
        let source = "(?<![\\p{L}\\p{N}])" + body + "(?![\\p{L}\\p{N}])"
        return try? NSRegularExpression(pattern: source, options: [.caseInsensitive])
    }

    /// "ShoutAbout" -> ["Shout", "About"]; "GPU" and "hello" stay whole.
    static func splitCamelCase(_ word: String) -> [String] {
        var parts: [String] = []
        var current = ""
        var previous: Character?
        for character in word {
            if let previous, previous.isLowercase, character.isUppercase, !current.isEmpty {
                parts.append(current)
                current = ""
            }
            current.append(character)
            previous = character
        }
        if !current.isEmpty { parts.append(current) }
        return parts
    }

    // MARK: Pass 2: sound-alike

    /// The smallest term (in letters) the sound-alike pass considers.
    static let minimumTermLetters = 4
    /// The shortest word that may differ from the term's spelling.
    static let minimumWordLetters = 3

    static func fixSoundAlikes(_ text: String, terms: [String]) -> String {
        var words = Word.split(text)
        guard !words.isEmpty else { return text }
        let candidates = terms.compactMap(Candidate.init)
            .sorted { ($0.wordCount, $0.letters) > ($1.wordCount, $1.letters) }

        // Words already spelled like a term are locked first, so "Sophia" stays "Sophia" when
        // both "Sophia" and "Sofia" are custom words.
        for candidate in candidates {
            for shape in candidate.shapes {
                forEachWindow(of: shape.count, in: words) { start in
                    let window = words[start..<(start + shape.count)]
                    guard zip(window, shape).allSatisfy({ $0.folded == $1.folded }) else { return false }
                    for index in window.indices { words[index].locked = true }
                    return true
                }
            }
        }

        var replacements: [(range: Range<String.Index>, text: String)] = []
        for candidate in candidates where candidate.letters >= minimumTermLetters {
            for shape in candidate.shapes {
                forEachWindow(of: shape.count, in: words) { start in
                    let window = Array(words[start..<(start + shape.count)])
                    guard matches(window, shape: shape, candidate: candidate) else { return false }
                    for index in start..<(start + shape.count) { words[index].locked = true }
                    let first = window[0]
                    var spelling = candidate.term
                    // A lowercase term ("faster-whisper") that starts a sentence keeps the capital.
                    if !candidate.isNameLike, first.isSentenceStart, first.isCapitalised {
                        spelling = spelling.prefix(1).uppercased() + spelling.dropFirst()
                    }
                    let last = window[window.count - 1]
                    replacements.append((range: first.range.lowerBound..<last.range.upperBound, text: spelling))
                    return true
                }
            }
        }
        guard !replacements.isEmpty else { return text }

        var output = ""
        var cursor = text.startIndex
        for replacement in replacements.sorted(by: { $0.range.lowerBound < $1.range.lowerBound }) {
            output += text[cursor..<replacement.range.lowerBound]
            output += replacement.text
            cursor = replacement.range.upperBound
        }
        output += text[cursor...]
        return output
    }

    /// Calls `body` for each run of `count` unlocked words joined only by spaces or hyphens;
    /// after a match (`body` returns true) the scan continues after the run.
    private static func forEachWindow(of count: Int, in words: [Word], _ body: (Int) -> Bool) {
        guard count > 0, words.count >= count else { return }
        var start = 0
        while start + count <= words.count {
            let indices = start..<(start + count)
            let free = indices.allSatisfy { !words[$0].locked }
            let joined = indices.dropFirst().allSatisfy { words[$0].joinsPrevious }
            if free, joined, body(start) {
                start += count
            } else {
                start += 1
            }
        }
    }

    private static func matches(_ window: [Word], shape: [Candidate.Part], candidate: Candidate) -> Bool {
        var anchors: [Word] = []
        var alikes: [Word] = []
        for (word, part) in zip(window, shape) {
            if word.folded == part.folded {
                anchors.append(word)
            } else if soundsAlike(word, part) {
                alikes.append(word)
            } else {
                return false
            }
        }
        guard !alikes.isEmpty else { return false }  // spelled right already (pass 1)

        let letters = window.reduce(0) { $0 + $1.folded.count }
        let slack = max(2, candidate.letters * 3 / 10)
        guard abs(letters - candidate.letters) <= slack else { return false }

        let distinctiveAnchor = anchors.contains { !SoundAlike.isCommonWord($0.folded) }
        for word in alikes {
            if SoundAlike.isCommonWord(word.folded) {
                guard candidate.isNameLike, word.isCapitalised, !word.isSentenceStart else { return false }
            } else {
                guard word.isCapitalised || distinctiveAnchor else { return false }
            }
        }
        return true
    }

    private static func soundsAlike(_ word: Word, _ part: Candidate.Part) -> Bool {
        guard word.isLettersOnly, part.isLettersOnly,
              word.folded.count >= minimumWordLetters, part.folded.count >= minimumWordLetters,
              !part.key.isEmpty, word.key == part.key
        else { return false }
        let edits = SoundAlike.distance(word.folded, part.folded)
        let longer = max(word.folded.count, part.folded.count)
        return edits <= 2 && edits * 5 <= longer * 2
    }

    // MARK: Pieces

    /// Lowercased, diacritics folded: how spellings are compared.
    static func fold(_ text: String) -> String {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: nil)
    }

    /// One word of the transcript: a run of letters and digits ("Sophia's" is "Sophia" + "s").
    private struct Word {
        var range: Range<String.Index>
        var folded: String
        var key: String
        var isCapitalised: Bool
        var isLettersOnly: Bool
        /// First word, or the first after . ! ? or a line break.
        var isSentenceStart: Bool
        /// Only spaces or hyphens since the previous word (it can be part of the same term).
        var joinsPrevious: Bool
        var locked = false

        static func split(_ text: String) -> [Word] {
            var words: [Word] = []
            var gapStart = text.startIndex
            var index = text.startIndex
            while index < text.endIndex {
                guard isWordCharacter(text[index]) else {
                    index = text.index(after: index)
                    continue
                }
                var end = index
                while end < text.endIndex, isWordCharacter(text[end]) {
                    end = text.index(after: end)
                }
                let raw = String(text[index..<end])
                let gap = text[gapStart..<index]
                let folded = VocabFixer.fold(raw)
                words.append(Word(
                    range: index..<end,
                    folded: folded,
                    key: SoundAlike.key(raw),
                    isCapitalised: raw.first?.isUppercase ?? false,
                    isLettersOnly: raw.allSatisfy(\.isLetter),
                    isSentenceStart: words.isEmpty || gap.contains { $0.isNewline || ".!?".contains($0) },
                    joinsPrevious: !words.isEmpty && gap.allSatisfy { $0 == "-" || ($0.isWhitespace && !$0.isNewline) }
                ))
                index = end
                gapStart = end
            }
            return words
        }

        static func isWordCharacter(_ character: Character) -> Bool {
            character.isLetter || character.isNumber
        }
    }

    /// A custom word, ready to compare: its words as written ("Better", "Shell") and, for
    /// camelCase, also as spoken ("ShoutAbout" -> "Shout", "About").
    private struct Candidate {
        struct Part {
            var folded: String
            var key: String
            var isLettersOnly: Bool
        }

        var term: String
        var shapes: [[Part]]
        var letters: Int
        /// Has a capital letter: a name or a brand ("Sofia", "Better Shell", "ShoutAbout").
        var isNameLike: Bool

        var wordCount: Int { shapes.map(\.count).max() ?? 0 }

        init?(_ term: String) {
            let written = Word.split(term).map { String(term[$0.range]) }
            guard !written.isEmpty else { return nil }
            let spoken = written.flatMap { VocabFixer.splitCamelCase($0) }
            func parts(_ words: [String]) -> [Part] {
                words.map { Part(folded: VocabFixer.fold($0), key: SoundAlike.key($0), isLettersOnly: $0.allSatisfy(\.isLetter)) }
            }
            self.term = term
            shapes = spoken.count == written.count ? [parts(written)] : [parts(spoken), parts(written)]
            letters = written.reduce(0) { $0 + VocabFixer.fold($1).count }
            isNameLike = term.contains { $0.isUppercase }
        }
    }
}
