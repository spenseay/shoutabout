import Foundation
@preconcurrency import Network

/// One-shot look at the current network path. Only "is Wi-Fi up?" matters for routing: the
/// app doesn't read the SSID (that needs Location permission and the Access Wi-Fi Information
/// entitlement); it probes the LAN URL instead.
enum NetworkPath {
    struct Snapshot: Sendable, Equatable {
        var onWiFi: Bool
        var connected: Bool
    }

    static func current() async -> Snapshot {
        await withCheckedContinuation { (continuation: CheckedContinuation<Snapshot, Never>) in
            let monitor = NWPathMonitor()
            let once = ResumeOnce()
            monitor.pathUpdateHandler = { path in
                guard once.claim() else { return }
                let snapshot = Snapshot(
                    onWiFi: path.status == .satisfied && (path.usesInterfaceType(.wifi) || path.usesInterfaceType(.wiredEthernet)),
                    connected: path.status == .satisfied
                )
                monitor.cancel()
                monitor.pathUpdateHandler = nil  // break the monitor <-> handler cycle
                continuation.resume(returning: snapshot)
            }
            monitor.start(queue: DispatchQueue(label: "fyi.shoutabout.path"))
        }
    }
}
