import Foundation

/// Sound-alike helpers for `VocabFixer`: a phonetic key (a simplified Metaphone), the edit
/// distance between two spellings, and a list of very common English words (which are never
/// rewritten into a custom word on sound alone).
enum SoundAlike {
    // MARK: Phonetic key

    /// A simplified Metaphone key (after Lawrence Philips, 1990) for one word. Words that sound
    /// alike in English usually share it: "Sophia" and "Sofia" -> "SF", "Better" and "Bettor"
    /// -> "BTR", "Shell" -> "XL", "Coppola" and "Copola" -> "KPL". Diacritics are folded
    /// ("Zoë" = "Zoe"); anything that isn't a Latin letter is ignored, so the key is "" for a
    /// number or a word in another script.
    ///
    /// Rules: duplicate letters count once (except C); a leading vowel becomes "A" and other
    /// vowels are dropped; initial KN/GN/PN/WR/AE drop their first letter, initial X sounds
    /// like S, initial WH like W; B is silent in a final MB; C -> X in CH and CIA, K in SCH,
    /// S before E/I/Y (silent in SCE/SCI/SCY), else K (CK -> K); D -> J in DGE/DGI/DGY, else T;
    /// GH is silent unless it starts the word or comes before a vowel (then K); G is silent in
    /// a final GN/GNED, J before E/I/Y, else K; H is silent after a vowel unless a vowel
    /// follows; PH -> F; Q -> K; S -> X in SH/SIO/SIA; T -> X in TIA/TIO, "0" in TH, silent in
    /// TCH; V -> F; W and Y only count before a vowel; X -> KS; Z -> S.
    static func key(_ word: String) -> String {
        let folded = word.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: nil)
        var w: [Character] = Array(folded.uppercased().filter { $0.isASCII && $0.isLetter })
        guard !w.isEmpty else { return "" }

        // Initial exceptions.
        let start = String(w.prefix(2))
        if ["KN", "GN", "PN", "WR", "AE"].contains(start) {
            w.removeFirst()
        } else if w[0] == "X" {
            w[0] = "S"
        } else if start == "WH" {
            w.remove(at: 1)
        }

        func at(_ index: Int) -> Character? {
            index >= 0 && index < w.count ? w[index] : nil
        }
        func isVowel(_ character: Character?) -> Bool {
            guard let character else { return false }
            return "AEIOU".contains(character)
        }

        var out = ""
        var i = 0
        while i < w.count {
            let c = w[i]
            let previous = at(i - 1)
            let next = at(i + 1)
            // Doubled letters sound once ("Coppola"), except C ("accent").
            if c != "C", c == previous {
                i += 1
                continue
            }
            switch c {
            case "A", "E", "I", "O", "U":
                if i == 0 { out.append("A") }
            case "B":
                if !(previous == "M" && i == w.count - 1) { out.append("B") }
            case "C":
                if next == "I", at(i + 2) == "A" {
                    out.append("X")
                } else if next == "H" {
                    out.append(previous == "S" ? "K" : "X")
                    i += 1
                } else if next == "E" || next == "I" || next == "Y" {
                    if previous != "S" { out.append("S") }
                } else if next == "K" {
                    out.append("K")
                    i += 1
                } else {
                    out.append("K")
                }
            case "D":
                if next == "G", let after = at(i + 2), "EIY".contains(after) {
                    out.append("J")
                    i += 2
                } else {
                    out.append("T")
                }
            case "G":
                if next == "H" {
                    if i == 0 || isVowel(at(i + 2)) { out.append("K") }
                    i += 1
                } else if next == "N", i + 2 == w.count || (i + 4 == w.count && at(i + 2) == "E" && at(i + 3) == "D") {
                    break  // silent: "sign", "signed"
                } else if next == "E" || next == "I" || next == "Y" {
                    out.append("J")
                } else {
                    out.append("K")
                }
            case "H":
                if !(isVowel(previous) && !isVowel(next)) { out.append("H") }
            case "K":
                if previous != "C" { out.append("K") }
            case "P":
                if next == "H" {
                    out.append("F")
                    i += 1
                } else {
                    out.append("P")
                }
            case "Q":
                out.append("K")
            case "S":
                if next == "H" {
                    out.append("X")
                    i += 1
                } else if next == "I", let after = at(i + 2), after == "O" || after == "A" {
                    out.append("X")
                } else {
                    out.append("S")
                }
            case "T":
                if next == "I", let after = at(i + 2), after == "O" || after == "A" {
                    out.append("X")
                } else if next == "H" {
                    out.append("0")
                    i += 1
                } else if !(next == "C" && at(i + 2) == "H") {
                    out.append("T")
                }
            case "V":
                out.append("F")
            case "W", "Y":
                if isVowel(next) { out.append(c) }
            case "X":
                out.append("KS")
            case "Z":
                out.append("S")
            default:
                out.append(c)  // F, J, L, M, N, R
            }
            i += 1
        }
        return out
    }

    // MARK: Spelling distance

    /// Levenshtein distance (insertions, deletions, substitutions) between two spellings.
    static func distance(_ a: String, _ b: String) -> Int {
        let a = Array(a)
        let b = Array(b)
        if a.isEmpty { return b.count }
        if b.isEmpty { return a.count }
        var previous = Array(0...b.count)
        var current = [Int](repeating: 0, count: b.count + 1)
        for i in 1...a.count {
            current[0] = i
            for j in 1...b.count {
                let cost = a[i - 1] == b[j - 1] ? 0 : 1
                current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            }
            swap(&previous, &current)
        }
        return previous[b.count]
    }

    // MARK: Common words

    /// True for a very common English word ("sofa", "shoot", "simple", "cloud"), compared
    /// lowercased with diacritics folded.
    static func isCommonWord(_ word: String) -> Bool {
        commonWords.contains(word.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil))
    }

    /// Everyday English: function words, the most frequent verbs, nouns, adjectives and adverbs,
    /// plus household words that sound like popular names ("sofa" ~ Sofia, "cloud" ~ Claude).
    static let commonWords: Set<String> = Set(commonWordList.split(separator: " ").map(String.init))

    private static let commonWordList = """
    a about above across act actually add after afternoon again against age ago agree ahead air all \
    allow almost alone along already also always am among amount an and angry animal another answer \
    any anyone anything anyway anywhere apple appear are area arm around arrive art as ask at attack \
    aunt autumn away baby back bad bag ball bank bar base bath be beach bear beat beautiful became \
    because become bed bedroom been beer before began begin behind being believe bell belong below \
    belt bench best bet better between big bike bill bird birthday bit black blank block blood blow \
    blue board boat body bone book born borrow boss both bottle bottom bought bowl box boy brain \
    bread break breakfast bridge bright bring broke brother brought brown build building burn bus \
    business busy but butter buy by cake call came camera can car card care careful carry case cat \
    catch cause cell center chair chance change charge cheap check cheese chicken child children \
    choose church city class clean clear climb clock close cloth clothes cloud club coat code coffee \
    cold collect college color come common company complete computer cook cool copy corner cost \
    could count country couple course court cousin cover cow crazy cream cross cry cup cut dad dance \
    dark data date daughter day dead deal dear decide deep desk did die different dinner direction \
    dirty do doctor does dog doing dollar done door double down draw dream dress drink drive drop dry \
    during each ear early earth easy eat edge egg eight either else email empty end enough enter \
    even evening event ever every everyone everything exactly example except excuse expect eye face \
    fact fall family far farm fast father favorite fear feel feet fell felt few field fight file fill \
    final find fine finger finish fire first fish fit five fix floor flower fly follow food foot for \
    forget fork form forward four free fresh friend from front fruit full fun funny game garden gas \
    gave get gift girl give glad glass go god going gold gone good got grade great green grew ground \
    group grow guess guy had hair half hall hand hang happen happy hard has hat hate have he head hear \
    heard heart heat heavy hello help her here hers high hill him his history hit hold hole holiday \
    home hope horse hospital hot hotel hour house how however huge human hundred hungry hurry hurt \
    husband i ice idea if important in inside instead interest into iron is island it its job join \
    joke juice jump just keep key kick kid kill kind king kitchen knew knife know lady lake land large \
    last late later laugh law lay lead learn least leave left leg less lesson let letter level lie \
    life light like line list listen little live long look lose loss lost lot loud love low luck lunch \
    machine made mail main make man many map mark market matter may maybe me meal mean meat meet \
    meeting member men message met middle might mile milk mind mine minute miss mister mom moment \
    money month moon more morning most mother mountain mouth move movie much music must my name near \
    neck need never new news next nice night nine no noise none noon nor north nose not note nothing \
    notice now number of off offer office often oh oil ok okay old on once one only open or orange \
    order other our out outside over own page paid pain paint pair paper parent park part party pass \
    past pay pen people perfect perhaps person phone photo pick picture piece pink place plan plane \
    plant plate play please plus pocket point police poor pop post pound power present pretty price \
    print problem project pull push put question quick quiet quite race radio rain raise ran rather \
    reach read ready real really reason red remember rent rest return rice rich ride right ring rise \
    river road rock room round row rule run sad safe said sale salt same sat save saw say school sea \
    season seat second see seem seen sell send sense sent set seven several shall shape share she \
    sheet shirt shoe shoot shop short should shout show shower shut sick side sign silly simple since \
    sing sister sit six size skin sky sleep slow small smell smile snow so soap sofa soft some someone \
    something sometimes son song soon sorry sort sound soup south space speak special spend sport \
    spring square stage stand star start station stay step still stone stop store story street \
    strong student study stuff such sugar suit summer sun supper sure surprise sweet swim table take \
    talk tall taste taxi tea teach team tell ten test text than thank thanks that the their them then \
    there these they thing think third this those though thought three through throw ticket tie time \
    tired to today together told tomorrow tonight too took tooth top total touch toward town toy \
    train travel tree trip trouble truck true try turn twelve twenty two type uncle under understand \
    until up upon us use used usual very view visit voice wait wake walk wall want war warm was wash \
    watch water way we wear weather wedding week weekend weight welcome well went were west wet what \
    wheel when where which while white who whole why wide wife will win window wine winter wish with \
    without woman women won wonder wood word work world worry would write wrong yard yeah year yellow \
    yes yesterday yet you young your
    """
}
