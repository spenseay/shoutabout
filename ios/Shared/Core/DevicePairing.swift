import Foundation

/// How a pairing request ended.
enum PairingOutcome: Equatable, Sendable {
    case approved(token: String, hub: PairedHubInfo)
    /// Someone clicked "Don't allow" on the PC.
    case denied
    /// Nobody answered within the request's lifetime (or the PC forgot the request).
    case expired
    case cancelled
    case failed(String)
}

/// A pairing problem, in words for the user.
struct PairingProblem: Error, Equatable, Sendable {
    var message: String
}

/// The v2 "tap your PC, click Allow" pairing (no QR code, no typing):
///
///  1. `GET /hello` on the address Bonjour resolved: is it a ShoutAbout PC that pairs?
///  2. `POST /pair/request` -> the PC shows "iPhone wants to dictate to this PC. Code 4821."
///  3. Poll `GET /pair/status` every second for up to 120 s until Allow / Don't allow.
///
/// Pure logic over `HubTransport`, with an injectable sleep, so it's unit-tested.
struct DevicePairing: Sendable {
    var transport: any HubTransport
    var pollInterval: TimeInterval = 1
    var maxWait: TimeInterval = 120
    /// Consecutive failed polls (Wi-Fi blip, PC restarting) before giving up.
    var maxConsecutiveErrors = 5
    var sleep: @Sendable (TimeInterval) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }

    /// Step 1: checks the address really is a ShoutAbout PC that accepts pairing.
    func hello(_ base: URL, timeout: TimeInterval = 4) async -> Result<HelloResponse, PairingProblem> {
        do {
            let hello = try await transport.hello(base, timeout: timeout)
            guard hello.isShoutAbout else {
                return .failure(PairingProblem(message: "That address isn't a ShoutAbout PC."))
            }
            if hello.pairing == false {
                return .failure(PairingProblem(
                    message: "Pairing is turned off on \(hello.name ?? "that PC"). Turn it on in ShoutAbout's settings on the PC."
                ))
            }
            return .success(hello)
        } catch {
            return .failure(Self.problem(for: HubFailure.from(error)))
        }
    }

    /// Step 2: asks the PC to show the approval prompt.
    func request(_ base: URL, deviceName: String, deviceID: String) async -> Result<PairRequestAccepted, PairingProblem> {
        do {
            let accepted = try await transport.requestPairing(base, body: PairRequestBody(deviceName: deviceName, deviceID: deviceID))
            guard !accepted.requestID.isEmpty, !accepted.code.isEmpty else {
                return .failure(PairingProblem(message: "Your PC's answer couldn't be read. Update ShoutAbout on the PC."))
            }
            return .success(accepted)
        } catch {
            return .failure(Self.problem(for: HubFailure.from(error)))
        }
    }

    /// Step 3: waits for Allow / Don't allow on the PC.
    func waitForApproval(_ base: URL, requestID: String, expiresIn: Int?) async -> PairingOutcome {
        let lifetime = min(TimeInterval(max(expiresIn ?? Int(maxWait), 1)), maxWait)
        let polls = max(1, Int((lifetime / max(pollInterval, 0.01)).rounded(.up)))
        var consecutiveErrors = 0
        for _ in 0..<polls {
            do {
                try await sleep(pollInterval)
            } catch {
                return .cancelled
            }
            if Task.isCancelled { return .cancelled }
            do {
                let status = try await transport.pairingStatus(base, requestID: requestID)
                consecutiveErrors = 0
                switch status {
                case .pending:
                    continue
                case .approved(let token, let hub):
                    return .approved(token: token, hub: hub)
                case .denied:
                    return .denied
                case .expired:
                    return .expired
                case .claimed:
                    return .failed("This request was already used. Try again.")
                case .unknown(let status):
                    return .failed("Your PC answered something this app doesn't understand (\(status)). Update ShoutAbout.")
                }
            } catch {
                let failure = HubFailure.from(error)
                // The PC no longer knows the request (it restarted, or it expired and was dropped).
                if failure.status == 404 { return .expired }
                consecutiveErrors += 1
                if consecutiveErrors >= maxConsecutiveErrors {
                    return .failed(Self.problem(for: failure).message)
                }
            }
        }
        return .expired
    }

    /// The words for a failed pairing call.
    static func problem(for failure: HubFailure) -> PairingProblem {
        let message: String
        switch failure {
        case .unreachable(let why):
            message = "Couldn't reach that PC (\(why)). Is it on the same Wi-Fi as this iPhone?"
        case .hub(let status, let detail), .gateway(let status, let detail):
            switch status {
            case 404, 405:
                message = "That PC has an older ShoutAbout without quick pairing. Update it, or scan a code instead."
            case 403:
                message = "That PC only pairs with phones on its home Wi-Fi."
            case 409:
                message = "That PC is already asking about another device. Answer it on the PC, then try again."
            case 422:
                message = "That PC didn't understand the request (\(detail))."
            case 429:
                message = "Too many tries. Wait a minute, then try again."
            default:
                message = "That PC answered with error \(status)."
            }
        case .badResponse:
            message = "That PC's answer couldn't be read. Update ShoutAbout on the PC."
        }
        return PairingProblem(message: message)
    }
}
