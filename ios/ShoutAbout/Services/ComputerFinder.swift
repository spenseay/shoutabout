import Foundation
@preconcurrency import Network
import Observation

/// Finds ShoutAbout PCs on the Wi-Fi with Bonjour (`_shoutabout._tcp`, Info.plist
/// `NSBonjourServices`) and resolves one to an `http://<ipv4>:<port>` base URL.
///
/// The first browse shows iOS's Local Network prompt (`NSLocalNetworkUsageDescription`). If the
/// user said no, NWBrowser waits with a PolicyDenied DNS error: `state` becomes
/// `.needsPermission` so the UI can point to Settings.
@MainActor
@Observable
final class ComputerFinder {
    enum State: Equatable {
        case idle
        case searching
        case needsPermission
        case failed(String)
    }

    private(set) var state: State = .idle
    private(set) var computers: [DiscoveredComputer] = []

    @ObservationIgnored private var browser: NWBrowser?
    @ObservationIgnored private var endpoints: [String: NWEndpoint] = [:]
    @ObservationIgnored private var isDemo = false

    /// DNS-SD "policy denied" (kDNSServiceErr_PolicyDenied): Local Network access is off.
    nonisolated private static let policyDenied: Int32 = -65570

    func start() {
        guard browser == nil, !isDemo else { return }
        let parameters = NWParameters()
        parameters.includePeerToPeer = false
        let browser = NWBrowser(for: .bonjourWithTXTRecord(type: "_shoutabout._tcp", domain: nil), using: parameters)
        browser.stateUpdateHandler = { [weak self] newState in
            let mapped = ComputerFinder.map(newState)
            let finder = self
            Task { @MainActor in finder?.apply(mapped) }
        }
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            let found = ComputerFinder.extract(results)
            let finder = self
            Task { @MainActor in finder?.update(found) }
        }
        self.browser = browser
        state = .searching
        browser.start(queue: DispatchQueue(label: "fyi.shoutabout.browser"))
    }

    func stop() {
        browser?.cancel()
        browser = nil
        if !isDemo { state = .idle }
    }

    /// Resolves a found PC to its address (IPv4, so the URL is a plain private IP).
    func baseURL(for computer: DiscoveredComputer) async -> URL? {
        guard let endpoint = endpoints[computer.id] else { return nil }
        return await EndpointResolver.baseURL(for: endpoint)
    }

    /// Looks for one PC by its installation id (its address changed: new DHCP lease).
    func findAddress(of hubID: String, timeout: Duration = .seconds(4)) async -> URL? {
        let startedHere = browser == nil
        start()
        defer { if startedHere { stop() } }
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if let match = computers.first(where: { $0.id == hubID }) {
                return await baseURL(for: match)
            }
            if state == .needsPermission { return nil }
            try? await Task.sleep(for: .milliseconds(200))
        }
        return nil
    }

    // MARK: Browser callbacks (they arrive on the browser's queue)

    private func apply(_ newState: State?) {
        guard let newState else { return }
        state = newState
    }

    private func update(_ found: [(DiscoveredComputer, NWEndpoint)]) {
        var map: [String: NWEndpoint] = [:]
        for (computer, endpoint) in found where map[computer.id] == nil {
            map[computer.id] = endpoint
        }
        endpoints = map
        computers = DiscoveredComputer.unique(found.map(\.0))
    }

    nonisolated private static func map(_ state: NWBrowser.State) -> State? {
        switch state {
        case .ready, .setup:
            return .searching
        case .waiting(let error), .failed(let error):
            if case .dns(let code) = error, code == policyDenied { return .needsPermission }
            if case .failed = state { return .failed(error.localizedDescription) }
            return nil
        case .cancelled:
            return nil
        @unknown default:
            return nil
        }
    }

    nonisolated private static func extract(_ results: Set<NWBrowser.Result>) -> [(DiscoveredComputer, NWEndpoint)] {
        results.compactMap { (result: NWBrowser.Result) -> (DiscoveredComputer, NWEndpoint)? in
            guard case let .service(name, _, _, _) = result.endpoint else { return nil }
            var txt: [String: String] = [:]
            if case let .bonjour(record) = result.metadata {
                txt = record.dictionary
            }
            return (DiscoveredComputer(serviceName: name, txt: txt), result.endpoint)
        }
    }

    #if DEBUG
    /// Screenshot scenes: show made-up PCs instead of browsing.
    func showDemo(_ list: [DiscoveredComputer]) {
        isDemo = true
        browser?.cancel()
        browser = nil
        computers = list
        state = .searching
    }
    #endif
}

/// Turns a Bonjour service endpoint into `http://<ipv4>:<port>` by opening (and immediately
/// closing) a TCP connection to it: Network.framework resolves the service on the way.
enum EndpointResolver {
    static func baseURL(for endpoint: NWEndpoint, timeout: TimeInterval = 5) async -> URL? {
        let parameters = NWParameters.tcp
        if let ip = parameters.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options {
            ip.version = .v4
        }
        let connection = NWConnection(to: endpoint, using: parameters)
        let queue = DispatchQueue(label: "fyi.shoutabout.resolve")
        let once = ResumeOnce()
        return await withCheckedContinuation { (continuation: CheckedContinuation<URL?, Never>) in
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    guard once.claim() else { return }
                    let url = connection.currentPath?.remoteEndpoint.flatMap(Self.url(for:))
                    connection.cancel()
                    continuation.resume(returning: url)
                case .failed, .cancelled:
                    guard once.claim() else { return }
                    connection.cancel()
                    continuation.resume(returning: nil)
                default:
                    break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) {
                guard once.claim() else { return }
                connection.cancel()
                continuation.resume(returning: nil)
            }
        }
    }

    static func url(for endpoint: NWEndpoint) -> URL? {
        guard case let .hostPort(host, port) = endpoint else { return nil }
        let hostText: String
        switch host {
        case .ipv4(let address):
            hostText = address.rawValue.map { String($0) }.joined(separator: ".")
        case .name(let name, _):
            hostText = name
        case .ipv6:
            return nil
        @unknown default:
            return nil
        }
        return URL(string: "http://\(hostText):\(port.rawValue)")
    }
}
