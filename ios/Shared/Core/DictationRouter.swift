import Foundation

/// Text written by this iPhone's own speech recognizer.
struct OnDeviceResult: Equatable, Sendable {
    var text: String
    var engine: String
}

/// On-device transcription (the app wraps `OfflineTranscriber`; tests use a mock).
protocol OnDeviceTranscribing: Sendable {
    func transcribe(_ audioURL: URL) async throws -> OnDeviceResult
}

/// One recording to deliver.
struct DictationRequest: Sendable {
    var audioURL: URL
    var target: DictationTarget
    /// The paired computer (nil when none is paired).
    var computer: ComputerLink?
    /// Wi-Fi or Ethernet is up. On cellular the LAN check is skipped (it can't work and would
    /// cost 1.5 s), unless the LAN address is a VPN one (Tailscale's 100.64.0.0/10).
    var onWiFi: Bool
    /// A fresh `/info` for this computer, so the dictation doesn't wait for it.
    var cachedInfo: HubInfo?
}

/// What the user sees while the router works.
enum DictationStage: Equatable, Sendable {
    case lookingForComputer
    case sendingAudio(Route)
    case transcribingOnPhone
    case pastingOnComputer

    var message: String {
        switch self {
        case .lookingForComputer: return "Knocking on your PC…"
        case .sendingAudio(.tunnel): return "Shouting through your tunnel…"
        case .sendingAudio: return "Your PC is listening…"
        case .transcribingOnPhone: return "Writing it down on this iPhone…"
        case .pastingOnComputer: return "Pasting on your PC…"
        }
    }
}

/// A delivered dictation.
struct DictationOutcome: Equatable, Sendable {
    enum Delivery: Equatable, Sendable {
        /// The computer pasted it into the focused window.
        case pastedOnComputer
        /// The app puts it on this iPhone's clipboard.
        case clipboard
    }

    var text: String
    var rawText: String?
    var delivery: Delivery
    /// The route the text travelled (`offline`: it never left this iPhone).
    var route: Route
    /// Plain-words note for the user, e.g. why it's on the clipboard.
    var notice: String?
    /// The PC's answer when the PC transcribed (timings, cleanup).
    var hubResponse: TranscribeResponse?
    /// Set when this iPhone wrote the text.
    var engine: String?
    /// `/info` fetched during this dictation, for the app to cache.
    var learnedInfo: HubInfo?

    var needsClipboard: Bool { delivery == .clipboard && !text.isEmpty }
}

/// The dictation couldn't produce any text (the recording is kept for Retry).
struct DictationFailure: Error, Equatable, Sendable {
    var message: String
}

/// Short notes shown when the text ends up somewhere other than planned.
enum DictationNotice {
    static let away = "Your PC isn't reachable, so it's on your clipboard."
    static let paused = "Your PC is paused, so it's on your clipboard."
    static let busy = "Your PC is busy, so it's on your clipboard."
    static let rejected = "Your PC didn't accept this iPhone, so it's on your clipboard. Connect again in Settings."
    static let tooLong = "Too long to paste in one go, so it's on your clipboard."
    static let notPasted = "Your PC didn't paste it, so it's on your clipboard."
    static let noComputer = "No computer is connected, so it's on your clipboard."

    /// The note for a failed hub call after which the text goes to the clipboard.
    static func forFailure(_ failure: HubFailure) -> String {
        if case .badResponse = failure { return busy }
        guard let status = failure.status else { return away }
        if case .gateway = failure { return away }
        switch status {
        case 401, 403: return rejected
        case 409: return paused
        case 413: return tooLong
        default: return busy
        }
    }
}

/// Decides where one recording goes and does it (the v2 phone routing):
///
///  - Target **Phone**: the clipboard. A paired PC that answers on the LAN and isn't slow
///    writes the text (`/transcribe?target=phone`, best quality); otherwise this iPhone does.
///  - Target **Computer**, PC answers on the LAN (`/hello`, ≤ 1.5 s; `/health` for older PCs):
///      speed != slow -> upload the audio to `/transcribe?target=pc` (the PC pastes);
///      speed == slow -> transcribe on this iPhone, `POST /paste` (faster on a CPU-only PC).
///  - PC unreachable: the owner's tunnel if it's set up (advanced), otherwise transcribe on this
///    iPhone and copy, telling the user "Your PC isn't reachable, so it's on your clipboard".
///
/// The words are never lost: any hub failure falls back to on-device transcription + clipboard.
/// Only when this iPhone can't transcribe either does it fail (the app keeps the audio).
struct DictationRouter: Sendable {
    var transport: any HubTransport
    var onDevice: any OnDeviceTranscribing
    /// The LAN reachability budget (`/hello`, then `/health` in what's left).
    var reachTimeout: TimeInterval = 1.5
    var infoTimeout: TimeInterval = 2

    typealias Progress = @Sendable (DictationStage) async -> Void

    func run(_ request: DictationRequest, progress: Progress = { _ in }) async -> Result<DictationOutcome, DictationFailure> {
        guard request.target == .pc else {
            return await runForPhone(request, progress: progress)
        }
        guard let computer = request.computer, computer.isUsable else {
            return await onDeviceToClipboard(request, notice: DictationNotice.noComputer, progress: progress)
        }

        var learned: HubInfo?
        if let lan = computer.lanEndpoint, request.onWiFi || URLRules.isVPNHost(lan.baseURL.host() ?? "") {
            await progress(.lookingForComputer)
            if await isReachable(lan.baseURL, expectedID: computer.hubID) {
                let attempt = await deliverOnLAN(lan, request: request, progress: progress)
                switch attempt {
                case .done(let outcome):
                    return .success(outcome)
                case .failed(let failure):
                    return .failure(failure)
                case .fallback(let notice, let info):
                    return await onDeviceToClipboard(request, notice: notice, learned: info, progress: progress)
                case .tryNext(let info):
                    learned = info
                }
            }
        }

        if let tunnel = computer.tunnel {
            await progress(.sendingAudio(.tunnel))
            do {
                let response = try await transport.transcribe(fileURL: request.audioURL, target: .pc, endpoint: tunnel)
                var outcome = Self.outcome(from: response, route: .tunnel)
                outcome.learnedInfo = learned
                return .success(outcome)
            } catch {
                let failure = HubFailure.from(error)
                let notice = RoutePlanner.action(after: failure) == .tryNext ? DictationNotice.away : DictationNotice.forFailure(failure)
                return await onDeviceToClipboard(request, notice: notice, learned: learned, progress: progress)
            }
        }

        return await onDeviceToClipboard(request, notice: DictationNotice.away, learned: learned, progress: progress)
    }

    // MARK: Phone target

    /// Phone target: the text always ends up on this iPhone's clipboard, written by the best
    /// recognizer at hand. A paired PC that answers on the LAN and isn't slow (or is an older PC
    /// without `/info`) transcribes it (`?target=phone`: the PC pastes nothing); otherwise (no
    /// PC, away, slow, or any PC error) this iPhone transcribes. No note either way: the user
    /// asked for the clipboard and gets it. The tunnel is never used for this.
    private func runForPhone(_ request: DictationRequest, progress: Progress) async -> Result<DictationOutcome, DictationFailure> {
        var learned: HubInfo?
        if let computer = request.computer, let lan = computer.lanEndpoint,
           request.onWiFi || URLRules.isVPNHost(lan.baseURL.host() ?? "") {
            await progress(.lookingForComputer)
            if await isReachable(lan.baseURL, expectedID: computer.hubID) {
                var info = request.cachedInfo
                if info == nil, let fetched = try? await transport.info(lan, timeout: infoTimeout) {
                    info = fetched
                    learned = fetched
                }
                if info?.speed != .slow {
                    await progress(.sendingAudio(.lan))
                    if let response = try? await transport.transcribe(fileURL: request.audioURL, target: .phone, endpoint: lan) {
                        return .success(DictationOutcome(
                            text: response.text,
                            rawText: response.rawText,
                            delivery: .clipboard,
                            route: .lan,
                            hubResponse: response,
                            learnedInfo: learned
                        ))
                    }
                }
            }
        }
        return await onDeviceToClipboard(request, notice: nil, learned: learned, progress: progress)
    }

    // MARK: Reachability

    /// `GET /hello` within the budget. A PC from before v2 answers 404 there, so `/health` gets
    /// the time that's left. When the pairing recorded the PC's id, a different PC at the same
    /// address (another home network, a new DHCP lease) counts as unreachable: the token is
    /// never sent to it.
    func isReachable(_ base: URL, expectedID: String?) async -> Bool {
        let started = ContinuousClock.now
        do {
            let hello = try await transport.hello(base, timeout: reachTimeout)
            guard hello.isShoutAbout else { return false }
            if let expectedID, let id = hello.id, id != expectedID { return false }
            return true
        } catch {
            let failure = HubFailure.from(error)
            guard failure.status == 404 else { return false }
            let remaining = reachTimeout - Self.seconds(since: started)
            guard remaining > 0.2 else { return false }
            do {
                try await transport.health(base, timeout: remaining)
                return true
            } catch {
                return false
            }
        }
    }

    // MARK: LAN delivery

    private enum Attempt {
        case done(DictationOutcome)
        case failed(DictationFailure)
        /// The PC answered but can't take it: transcribe here and copy, with this note.
        case fallback(notice: String, info: HubInfo?)
        /// The PC stopped answering mid-way: try the tunnel, then on-device.
        case tryNext(info: HubInfo?)
    }

    private func deliverOnLAN(_ endpoint: HubEndpoint, request: DictationRequest, progress: Progress) async -> Attempt {
        var info = request.cachedInfo
        var learned: HubInfo?
        if info == nil, let fetched = try? await transport.info(endpoint, timeout: infoTimeout) {
            info = fetched
            learned = fetched
        }

        if let info, info.prefersText {
            return await transcribeHereThenPaste(endpoint, request: request, learned: learned, progress: progress)
        }

        await progress(.sendingAudio(.lan))
        do {
            let response = try await transport.transcribe(fileURL: request.audioURL, target: .pc, endpoint: endpoint)
            var outcome = Self.outcome(from: response, route: .lan)
            outcome.learnedInfo = learned
            return .done(outcome)
        } catch {
            let failure = HubFailure.from(error)
            if RoutePlanner.action(after: failure) == .tryNext {
                return .tryNext(info: learned)
            }
            return .fallback(notice: DictationNotice.forFailure(failure), info: learned)
        }
    }

    /// The slow-PC path: this iPhone writes the text, the PC only pastes it.
    private func transcribeHereThenPaste(
        _ endpoint: HubEndpoint,
        request: DictationRequest,
        learned: HubInfo?,
        progress: Progress
    ) async -> Attempt {
        await progress(.transcribingOnPhone)
        let local: OnDeviceResult
        do {
            local = try await onDevice.transcribe(request.audioURL)
        } catch {
            return .failed(DictationFailure(message: Self.onDeviceMessage(error, away: false)))
        }
        func clipboard(_ notice: String?) -> Attempt {
            .done(DictationOutcome(
                text: local.text,
                delivery: .clipboard,
                route: .offline,
                notice: local.text.isEmpty ? nil : notice,
                engine: local.engine,
                learnedInfo: learned
            ))
        }
        if local.text.isEmpty { return clipboard(nil) }
        if local.text.count > PasteBody.maxLength { return clipboard(DictationNotice.tooLong) }

        await progress(.pastingOnComputer)
        do {
            let response = try await transport.paste(local.text, endpoint: endpoint)
            if response.pasted == false { return clipboard(DictationNotice.notPasted) }
            return .done(DictationOutcome(
                text: local.text,
                delivery: .pastedOnComputer,
                route: .lan,
                engine: local.engine,
                learnedInfo: learned
            ))
        } catch {
            return clipboard(DictationNotice.forFailure(HubFailure.from(error)))
        }
    }

    // MARK: On-device

    private func onDeviceToClipboard(
        _ request: DictationRequest,
        notice: String?,
        learned: HubInfo? = nil,
        progress: Progress
    ) async -> Result<DictationOutcome, DictationFailure> {
        await progress(.transcribingOnPhone)
        do {
            let local = try await onDevice.transcribe(request.audioURL)
            return .success(DictationOutcome(
                text: local.text,
                delivery: .clipboard,
                route: .offline,
                notice: local.text.isEmpty ? nil : notice,
                engine: local.engine,
                learnedInfo: learned
            ))
        } catch {
            let away = notice != nil && request.target == .pc
            return .failure(DictationFailure(message: Self.onDeviceMessage(error, away: away)))
        }
    }

    // MARK: Helpers

    /// The PC transcribed: pasted when it says so, otherwise the text goes to the clipboard.
    static func outcome(from response: TranscribeResponse, route: Route) -> DictationOutcome {
        let pasted = response.pasted ?? false
        let text = response.text
        return DictationOutcome(
            text: text,
            rawText: response.rawText,
            delivery: pasted ? .pastedOnComputer : .clipboard,
            route: route,
            notice: pasted || text.isEmpty ? nil : DictationNotice.notPasted,
            hubResponse: response
        )
    }

    static func onDeviceMessage(_ error: any Error, away: Bool) -> String {
        let reason = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        if away {
            return "Your PC isn't reachable, and this iPhone couldn't write it down: \(reason)"
        }
        return "This iPhone couldn't write it down: \(reason)"
    }

    static func seconds(since start: ContinuousClock.Instant) -> TimeInterval {
        let (seconds, attoseconds) = (ContinuousClock.now - start).components
        return TimeInterval(seconds) + TimeInterval(attoseconds) / 1e18
    }
}
