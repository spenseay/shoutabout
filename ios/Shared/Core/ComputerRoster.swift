import Foundation

/// Short names for computers. Windows makes up names like "DESKTOP-Q0DB401" for a new PC; they
/// say little, so they show as "Desktop" / "Laptop" (newer PCs already report that). A
/// nickname set on this iPhone always wins.
enum ComputerNames {
    /// "DESKTOP-Q0DB401" -> "Desktop", "laptop-ab12cd3" -> "Laptop"; any other name as it is
    /// (trimmed).
    static func pretty(_ raw: String) -> String {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let upper = name.uppercased()
        for (prefix, word) in [("DESKTOP-", "Desktop"), ("LAPTOP-", "Laptop")] where upper.hasPrefix(prefix) {
            let rest = upper.dropFirst(prefix.count)
            if rest.count == 7, rest.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) }) {
                return word
            }
        }
        return name
    }

    /// What a computer is called on this iPhone: the nickname, else the PC's own name made
    /// short, else "Computer".
    static func display(nickname: String?, name: String) -> String {
        if let nickname = nickname.nonBlank { return nickname }
        let short = pretty(name)
        return short.isEmpty ? "Computer" : short
    }

    /// A display name that's already taken ("Desktop", "Desktop") gets " 2", " 3" on the later
    /// ones, so every computer can be told apart. Case-insensitive.
    static func distinct(_ names: [String]) -> [String] {
        var taken: Set<String> = []
        var result: [String] = []
        for name in names {
            var candidate = name
            var number = 2
            while taken.contains(candidate.lowercased()) {
                candidate = "\(name) \(number)"
                number += 1
            }
            taken.insert(candidate.lowercased())
            result.append(candidate)
        }
        return result
    }
}

/// One computer to pick from (the main screen's switch, Settings).
struct ComputerChoice: Identifiable, Equatable, Sendable {
    var id: String
    /// Short and distinct ("Desktop", "Laptop", "Desktop 2").
    var name: String
    /// The PC's own name ("DESKTOP-Q0DB401"), to tell computers apart in Settings.
    var reportedName: String
    /// It has a token on this iPhone, so dictation can go there.
    var usable: Bool
    /// The older code pairing (QR), standing in as the computer when no other is paired.
    var isCodePairing: Bool = false
}

/// The computers this iPhone is paired with (Bonjour + Allow on the PC), which one "Computer"
/// means right now, and which one the older code pairing belongs to. Stored in the App Group
/// (`SharedSettings.roster`); each computer's device token is in the keychain under its id.
/// Pure logic, unit-tested.
struct ComputerRoster: Equatable, Sendable {
    /// The choice id of the code pairing when it is the only computer.
    static let codePairingID = "code-pairing"
    static let maxNicknameLength = 40

    /// In the order they were added (a repeated name gets " 2" on the later one).
    var computers: [PairedComputer] = []
    var selectedID: String?
    /// The computer the older code pairing (QR: hub token, tunnel) belongs to. Its tunnel only
    /// ever reaches that PC, so it's never used for another one.
    var codePairingOwnerID: String?

    /// The computer dictation goes to ("Computer" in the switch, the intents, Control Center).
    var selected: PairedComputer? {
        guard let selectedID else { return nil }
        return computer(selectedID)
    }

    func computer(_ id: String) -> PairedComputer? {
        computers.first { $0.id == id }
    }

    /// Keeps the selection and the code pairing's owner on computers that exist: with no
    /// valid selection, the first computer is selected.
    mutating func normalize() {
        if selected == nil { selectedID = computers.first?.id }
        if let owner = codePairingOwnerID, computer(owner) == nil { codePairingOwnerID = nil }
    }

    /// Short, distinct names by computer id.
    var displayNames: [String: String] {
        var result: [String: String] = [:]
        for (computer, name) in zip(computers, ComputerNames.distinct(computers.map(\.displayName))) {
            result[computer.id] = name
        }
        return result
    }

    func displayName(for id: String) -> String? {
        displayNames[id]
    }

    /// Every computer, in order, with its distinct name.
    func choices(usable usableIDs: Set<String>) -> [ComputerChoice] {
        var result: [ComputerChoice] = []
        for (computer, name) in zip(computers, ComputerNames.distinct(computers.map(\.displayName))) {
            result.append(ComputerChoice(
                id: computer.id,
                name: name,
                reportedName: computer.name,
                usable: usableIDs.contains(computer.id)
            ))
        }
        return result
    }

    /// A computer paired, or the same PC paired again: a new one goes at the end, the same one
    /// keeps its place and nickname and takes the new name and address. Either way it becomes
    /// the selected one. The code pairing, when no computer has it, goes with it if it's the
    /// same PC (the code's address). Never on a guess: its tunnel would reach another PC.
    mutating func add(_ computer: PairedComputer, codePairing: HubConfiguration?) {
        if let index = computers.firstIndex(where: { $0.id == computer.id }) {
            var updated = computer
            updated.nickname = computers[index].nickname
            computers[index] = updated
        } else {
            computers.append(computer)
        }
        selectedID = computer.id
        if let codePairing, codePairing.isPaired, codePairingOwnerID == nil,
           Self.sameHost(codePairing.lanURL, computer.lanURL) {
            codePairingOwnerID = computer.id
        }
    }

    /// Removes a computer. When it was the selected one, the first one left is selected (none
    /// when it was the last). True when it had the code pairing, which goes with it.
    @discardableResult
    mutating func remove(_ id: String) -> Bool {
        computers.removeAll { $0.id == id }
        let hadCodePairing = codePairingOwnerID == id
        if hadCodePairing { codePairingOwnerID = nil }
        if selectedID == id { selectedID = computers.first?.id }
        return hadCodePairing
    }

    /// A nickname on this iPhone; empty brings back the PC's own name.
    mutating func rename(_ id: String, to nickname: String) {
        guard let index = computers.firstIndex(where: { $0.id == id }) else { return }
        let trimmed = nickname.trimmingCharacters(in: .whitespacesAndNewlines)
        computers[index].nickname = trimmed.isEmpty ? nil : String(trimmed.prefix(Self.maxNicknameLength))
    }

    /// True when the selection changed.
    @discardableResult
    mutating func select(_ id: String) -> Bool {
        guard computer(id) != nil, selectedID != id else { return false }
        selectedID = id
        return true
    }

    /// The PC said its name (`/hello`, `/info`): kept when it changed. Blank names are ignored.
    @discardableResult
    mutating func noteName(_ reported: String?, for id: String) -> Bool {
        guard let name = reported.nonBlank,
              let index = computers.firstIndex(where: { $0.id == id }),
              computers[index].name != name
        else { return false }
        computers[index].name = name
        return true
    }

    /// The same PC answered at a new address (a new DHCP lease).
    @discardableResult
    mutating func noteAddress(_ url: URL, for id: String) -> Bool {
        guard let index = computers.firstIndex(where: { $0.id == id }), computers[index].lanURL != url else {
            return false
        }
        computers[index].lanURL = url
        return true
    }

    /// The code pairing was just set up (QR or the form): it belongs to the computer at the
    /// code's address, or to none (then it's only used when no computer is paired). Never on a
    /// guess, so its tunnel never reaches a PC other than the selected one.
    mutating func assignCodePairing(lanURL: URL?) {
        codePairingOwnerID = computers.first(where: { Self.sameHost($0.lanURL, lanURL) })?.id
    }

    /// The single computer from the version before several were possible becomes the first
    /// (and selected) one, with the code pairing if there is one. Running it again changes
    /// nothing.
    mutating func adopt(_ old: PairedComputer, hasCodePairing: Bool) {
        if computer(old.id) == nil { computers.insert(old, at: 0) }
        if selected == nil { selectedID = old.id }
        if hasCodePairing, codePairingOwnerID == nil { codePairingOwnerID = old.id }
    }

    static func sameHost(_ a: URL?, _ b: URL?) -> Bool {
        guard let first = a?.host()?.lowercased(), let second = b?.host()?.lowercased(), !first.isEmpty else {
            return false
        }
        return first == second
    }
}
