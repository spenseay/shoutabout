import Foundation

/// Darwin notifications between the app and the keyboard (`KeyboardSignal`). They carry no data
/// (the App Group has it) and reach every process observing the name; one that's suspended
/// misses them, which is why the app also writes a heartbeat (`KeyboardAppState`).
enum KeyboardSignals {
    /// Tells the other side.
    static func post(_ signal: KeyboardSignal) {
        CFNotificationCenterPostNotification(
            CFNotificationCenterGetDarwinNotifyCenter(),
            CFNotificationName(signal.rawValue as CFString),
            nil,
            nil,
            true
        )
    }

    /// Calls `handler` on the main actor whenever another process posts one of `signals`.
    /// Each name is observed once per process; a later call replaces the handler (the
    /// keyboard's newest controller takes over from the one before it).
    @MainActor
    static func observe(_ signals: [KeyboardSignal], handler: @escaping @MainActor (KeyboardSignal) -> Void) {
        KeyboardSignalRouter.handler = handler
        for signal in signals {
            guard !KeyboardSignalRouter.observed.contains(signal) else { continue }
            KeyboardSignalRouter.observed.insert(signal)
            addDarwinObserver(signal.rawValue)
        }
    }

    /// Nonisolated on purpose: the callback is a C function pointer, so it can't capture
    /// anything or belong to an actor. It looks the name up and hops to the main actor.
    private static func addDarwinObserver(_ observedName: String) {
        CFNotificationCenterAddObserver(
            CFNotificationCenterGetDarwinNotifyCenter(),
            nil,
            { _, _, name, _, _ in
                guard let cfName = name?.rawValue,
                      let signal = KeyboardSignal(rawValue: cfName as String)
                else { return }
                Task { @MainActor in
                    KeyboardSignalRouter.handler?(signal)
                }
            },
            observedName as CFString,
            nil,
            .deliverImmediately
        )
    }
}

/// Where the Darwin callbacks land.
@MainActor
enum KeyboardSignalRouter {
    static var handler: (@MainActor (KeyboardSignal) -> Void)?
    static var observed: Set<KeyboardSignal> = []
}
