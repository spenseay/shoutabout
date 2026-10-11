import SwiftUI
import UIKit

/// Every brand token in one place: colours, type, spacing, radii, mouth geometry and motion.
/// Source: the owner's brand sheet (palette Shout Yellow / Black / Tongue Red / Teeth, Anton +
/// Archivo, the open-mouth mark, saTalk / saPunch). Change values here, not in views.
///
/// Two values can't live in code and mirror these tokens by hand:
/// - `Resources/Assets.xcassets/LaunchBackground.colorset` (launch screen: ground colours)
/// - the app icons, rendered by `ios/scripts/make_icons.py` from the same geometry.
enum Theme {
    // MARK: Palette (raw brand colours)

    enum Palette {
        /// Ground / background. #FFE600
        static let shoutYellow = Color(hex: 0xFFE600)
        /// Type, mouth / throat. #0A0A0A
        static let black = Color(hex: 0x0A0A0A)
        /// Accent, the tongue. #FF1A1A
        static let tongueRed = Color(hex: 0xFF1A1A)
        /// Highlight, the teeth. #FFFFFF
        static let teeth = Color(hex: 0xFFFFFF)
        /// "Starting" state grey. #8C8C84
        static let startingGrey = Color(hex: 0x8C8C84)
        /// Dark surfaces. #202020
        static let darkTaskbar = Color(hex: 0x202020)
        /// Paper / light cards. #FAF9F5
        static let paper = Color(hex: 0xFAF9F5)

        // Alternative "loud" palettes from the sheet (seasonal / beta variants).
        static let loudBlue = Color(hex: 0x1A2BFF)
        static let loudMagenta = Color(hex: 0xFF2E9A)
        static let loudOrange = Color(hex: 0xFF5A00)
        static let loudGreen = Color(hex: 0x3DFF3D)
    }

    // MARK: Semantic colours (light / dark aware)

    enum Colors {
        /// Screen ground: yellow in light mode, black in dark mode (like the Dark app icon).
        static let ground = Color(light: Palette.shoutYellow, dark: Palette.black)
        /// Primary type on the ground.
        static let ink = Color(light: Palette.black, dark: Palette.shoutYellow)
        /// Secondary type on the ground.
        static let inkMuted = Color(light: Palette.black.opacity(0.62), dark: Palette.shoutYellow.opacity(0.7))
        /// Cards sitting on the ground.
        static let card = Color(light: Palette.paper, dark: Palette.darkTaskbar)
        static let cardInk = Color(light: Palette.black, dark: Palette.paper)
        static let cardInkMuted = Color(light: Palette.black.opacity(0.55), dark: Palette.paper.opacity(0.6))
        /// The big record tile (yellow, like the app icon; red when a dictation failed).
        static let tile: Color = Palette.shoutYellow
        static let tileRecording: Color = Palette.shoutYellow
        static let tileError = Palette.tongueRed
        static let tileStarting = Palette.startingGrey
        /// Accent for chips, toggles and the "live" dot.
        static let accent = Palette.tongueRed
        /// Control tint on system screens (Settings): black on light, yellow on dark.
        static let tint = Color(light: Palette.black, dark: Palette.shoutYellow)
        static let success = Palette.black
        static let warning = Palette.tongueRed
        /// Pills (Computer | Phone switch).
        static let pillOn = Color(light: Palette.black, dark: Palette.shoutYellow)
        static let pillOnInk = Color(light: Palette.shoutYellow, dark: Palette.black)
        static let pillOffInk = Color(light: Palette.black, dark: Palette.shoutYellow)
        /// The big mouth button: the app icon, a yellow tile with the primary mark, in every
        /// mode. A thin ink stroke keeps it reading as a button where it sits on yellow.
        static let button: Color = Palette.shoutYellow
        static let buttonStroke: Color = Palette.black
        static let buttonShimmer: Color = Palette.teeth.opacity(0.5)
        /// Primary buttons on the ground (onboarding).
        static let primaryButton = Color(light: Palette.black, dark: Palette.shoutYellow)
        static let primaryButtonInk = Color(light: Palette.shoutYellow, dark: Palette.black)
    }

    // MARK: Mouth palette

    /// The app draws only the primary mark (no lip ring): black throat, white teeth, red
    /// tongue, sitting on a yellow tile (red when a dictation failed).
    struct MouthStyle: Sendable {
        var throat: Color
        var teeth: Color
        var tongue: Color

        /// Primary mark: black throat, white teeth, red tongue.
        static let standard = MouthStyle(throat: Palette.black, teeth: Palette.teeth, tongue: Palette.tongueRed)
    }

    // MARK: Type

    enum FontName {
        static let display = "Anton-Regular"
        static let bodyMedium = "Archivo-Medium"
        static let bodyBold = "Archivo-Bold"
    }

    enum Typography {
        /// Anton is bundled (UIAppFonts); fall back to a heavy condensed system font if it
        /// failed to register (e.g. in the widget extension, which doesn't bundle fonts).
        static let hasDisplayFont = UIFont(name: FontName.display, size: 12) != nil
        static let hasBodyFont = UIFont(name: FontName.bodyMedium, size: 12) != nil

        /// Headlines / logo. Always set in UPPERCASE (use `.shoutHeadline(size)`).
        static func display(_ size: CGFloat, relativeTo style: Font.TextStyle = .largeTitle) -> Font {
            if hasDisplayFont {
                return .custom(FontName.display, size: size, relativeTo: style)
            }
            return .system(size: size, weight: .black).width(.condensed)
        }

        /// Body and UI (Archivo 500).
        static func body(_ size: CGFloat = 17, relativeTo style: Font.TextStyle = .body) -> Font {
            hasBodyFont ? .custom(FontName.bodyMedium, size: size, relativeTo: style) : .system(size: size, weight: .medium)
        }

        /// Emphasis (Archivo 700).
        static func bold(_ size: CGFloat = 17, relativeTo style: Font.TextStyle = .body) -> Font {
            hasBodyFont ? .custom(FontName.bodyBold, size: size, relativeTo: style) : .system(size: size, weight: .bold)
        }

        /// Labels / captions (the sheet uses JetBrains Mono; system mono stands in).
        static func mono(_ size: CGFloat = 12) -> Font {
            .system(size: size, weight: .semibold, design: .monospaced)
        }

        /// Anton letter-spacing: -0.01em.
        static func displayTracking(_ size: CGFloat) -> CGFloat { -0.01 * size }
        /// Mono label tracking: 0.1em.
        static func monoTracking(_ size: CGFloat) -> CGFloat { 0.1 * size }
        /// Anton line height ~0.86: stacked headline lines overlap by this fraction of the size.
        static let displayLineOverlap: CGFloat = 0.14
    }

    // MARK: Spacing / radii

    enum Spacing {
        static let xxs: CGFloat = 2
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
        static let huge: CGFloat = 48
    }

    enum Radius {
        /// Tiles (record button, tray-style squares): 23% of the tile's side.
        static let tileFraction: CGFloat = 0.23
        static let card: CGFloat = 22
        static let chip: CGFloat = 10
    }

    enum Size {
        static let recordTileMax: CGFloat = 280
        static let lockupFont: CGFloat = 76
        static let borderWidth: CGFloat = 3
    }

    // MARK: Mouth geometry (relative to the mouth box W x H; reference box 170 x 130)

    enum Mouth {
        /// OPEN: H / W = 130 / 170.
        static let openRatio: CGFloat = 130.0 / 170.0
        /// AJAR: H / W = 72 / 170 (no tongue).
        static let ajarRatio: CGFloat = 72.0 / 170.0
        /// Where the top and bottom half-ellipses meet, from the top, as a fraction of H.
        static let seam: CGFloat = 0.42
        /// Teeth band height as a fraction of H.
        static let teeth: CGFloat = 0.20
        /// Tongue ellipse: centre (0.50W, 0.93H), radii (0.22W, 0.31H).
        static let tongueCenterY: CGFloat = 0.93
        static let tongueRadiusX: CGFloat = 0.22
        static let tongueRadiusY: CGFloat = 0.31
        /// SHUT pill relative to the OPEN mouth width (62% and 12.5% of a tile whose mouth is 78%).
        static let shutWidth: CGFloat = 0.62 / 0.78
        static let shutHeight: CGFloat = 0.125 / 0.78
        /// Mouth width as a fraction of an icon tile.
        static let tileFraction: CGFloat = 0.78
        /// The zipping mouth ("done") in the app's tile, as in the owner's zip design: a little
        /// smaller, so the slider parked at its left stays inside the tile.
        static let zipTileFraction: CGFloat = 0.70
    }

    // MARK: Motion

    enum Motion {
        /// saTalk: mouth height keyframes (px of 130) over 1.6 s, ease-in-out, looping.
        static let talkDuration: Double = 1.6
        static let talkHeights: [CGFloat] = [130, 58, 118, 40, 130, 84, 124, 46, 108, 130]
        static let talkStops: [Double] = [0, 0.10, 0.22, 0.32, 0.44, 0.54, 0.64, 0.74, 0.86, 1.0]

        /// saTalk keyframe `index` (1...9) as a fraction of the OPEN height.
        static func talkScale(_ index: Int) -> CGFloat { talkHeights[index] / 130 }

        /// Seconds from keyframe `index - 1` to `index`.
        static func talkStep(_ index: Int) -> Double { (talkStops[index] - talkStops[index - 1]) * talkDuration }

        /// saPunch: 0/38/60/100% rest; 44% scale 1.07 rotate -2°; 50% scale 0.98 rotate 1°.
        static let punchDuration: Double = 1.6
        static let punchPeakScale: CGFloat = 1.07
        static let punchPeakAngle: Double = -2
        static let punchReboundScale: CGFloat = 0.98
        static let punchReboundAngle: Double = 1

        /// Mic level -> mouth: quietest and loudest mouth heights (as a fraction of OPEN).
        static let micMinOpen: CGFloat = 40.0 / 130.0
        static let micMaxOpen: CGFloat = 1.0
        /// Mic level (0...1) that counts as a loud peak (triggers a punch).
        static let loudPeak: Double = 0.82
        static let meterInterval: Duration = .milliseconds(50)
    }
}

/// Looping animations (breathing, talking, shimmer) are frozen on a still frame in the Debug
/// screenshot runs: XCUITest waits for an app to go idle, and a forever-animation never does.
enum MotionPolicy {
    static let frozen: Bool = {
        #if DEBUG
        return UserDefaults.standard.string(forKey: "ScreenshotScene") != nil
        #else
        return false
        #endif
    }()
}

// MARK: - Helpers

extension Color {
    /// 0xRRGGBB.
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }

    /// A colour that follows the light/dark appearance.
    init(light: Color, dark: Color) {
        self.init(uiColor: UIColor { traits in
            UIColor(traits.userInterfaceStyle == .dark ? dark : light)
        })
    }
}

extension View {
    /// Anton, uppercase, tight tracking: the brand headline style.
    func shoutHeadline(_ size: CGFloat, color: Color = Theme.Colors.ink) -> some View {
        self
            .font(Theme.Typography.display(size))
            .tracking(Theme.Typography.displayTracking(size))
            .textCase(.uppercase)
            .foregroundStyle(color)
    }

    /// Small uppercase mono label (chips, captions).
    func shoutLabel(_ size: CGFloat = 11, color: Color = Theme.Colors.inkMuted) -> some View {
        self
            .font(Theme.Typography.mono(size))
            .tracking(Theme.Typography.monoTracking(size))
            .textCase(.uppercase)
            .foregroundStyle(color)
    }
}
