import UIKit

/// Opens a URL (the ShoutAbout app) from the keyboard.
///
/// A keyboard can't open URLs the usual way: `extensionContext.open` does nothing for keyboards,
/// and `UIApplication.shared` / `open(_:options:completionHandler:)` are unavailable to app
/// extensions at compile time. So this walks the responder chain to the UIApplication object and
/// calls `open(_:options:completionHandler:)` on it through the Objective-C runtime, as keyboard
/// apps that open their own app do since iOS 18 (the older `openURL:` stopped working then).
/// Kept to this one function: if a later iOS stops it, only the "no live session" start needs
/// another way (the user opens the app instead).
@MainActor
enum AppOpener {
    /// True when the call was made (not whether iOS opened the app).
    @discardableResult
    static func open(_ url: URL, from responder: UIResponder) -> Bool {
        let selector = NSSelectorFromString("openURL:options:completionHandler:")
        var current: UIResponder? = responder
        while let candidate = current {
            if let application = candidate as? UIApplication,
               application.responds(to: selector),
               let implementation = application.method(for: selector) {
                // -[UIApplication openURL:options:completionHandler:] (receiver, _cmd, url,
                // options, completion). A nil completion is allowed.
                typealias OpenURL = @convention(c) (AnyObject, Selector, NSURL, NSDictionary, AnyObject?) -> Void
                let open = unsafeBitCast(implementation, to: OpenURL.self)
                open(application, selector, url as NSURL, NSDictionary(), nil)
                return true
            }
            current = candidate.next
        }
        return false
    }
}
