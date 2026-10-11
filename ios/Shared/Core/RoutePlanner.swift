import Foundation

/// Decides which routes to try, in order, and what to do when one fails.
///
/// Unlike the iOS Shortcut (which has no try/catch and must pick LAN vs tunnel from the Wi-Fi
/// name up front), the app tries the LAN first whenever it is on Wi-Fi and falls back. The LAN
/// attempt starts with an unauthenticated `GET /health` probe with a short timeout, so the hub
/// token is never sent over plain HTTP to whatever device happens to own that IP on a foreign
/// network unless something answers like a ShoutAbout hub there.
enum RoutePlanner {
    struct Inputs: Equatable, Sendable {
        var hasLANURL: Bool
        var hasPublicURL: Bool
        var onWiFi: Bool
        var preferLAN: Bool = true
        /// The "Offline" switch: never contact the PC.
        var forceOffline: Bool = false
        /// Fall back to on-device transcription when the PC can't be reached.
        var offlineFallback: Bool = true
    }

    /// Ordered routes to attempt for one dictation.
    static func candidates(_ inputs: Inputs) -> [Route] {
        if inputs.forceOffline { return [.offline] }
        var routes: [Route] = []
        if inputs.onWiFi {
            if inputs.preferLAN {
                if inputs.hasLANURL { routes.append(.lan) }
                if inputs.hasPublicURL { routes.append(.tunnel) }
            } else {
                if inputs.hasPublicURL { routes.append(.tunnel) }
                if inputs.hasLANURL { routes.append(.lan) }
            }
        } else {
            if inputs.hasPublicURL { routes.append(.tunnel) }
            // No public URL: the LAN URL may still work over a VPN such as Tailscale
            // (the hub accepts 100.64.0.0/10 as LAN).
            else if inputs.hasLANURL { routes.append(.lan) }
        }
        if inputs.offlineFallback { routes.append(.offline) }
        return routes
    }

    enum FailureAction: Equatable, Sendable {
        /// Try the next route in the list (the PC may be reachable another way).
        case tryNext
        /// The hub answered but can't help right now: skip the other hub routes, go on-device.
        case skipToOffline
        /// A definitive answer (bad token, too long, bad audio): show it, don't retry.
        case stop
    }

    static func action(after failure: HubFailure) -> FailureAction {
        switch failure {
        case .unreachable:
            return .tryNext
        case .gateway:
            // Cloudflare/tunnel/proxy pages: the PC wasn't reached on this route.
            return .tryNext
        case .hub(let status, _):
            switch status {
            case 503, 500, 502, 504: return .skipToOffline
            case 429: return .skipToOffline
            default: return .stop
            }
        case .badResponse:
            return .stop
        }
    }
}
