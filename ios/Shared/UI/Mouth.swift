import SwiftUI

// The ShoutAbout "open mouth" mark, drawn from the brand geometry (Theme.Mouth):
//
//   outline  top half-ellipse (rx = W/2, ry = 0.42H) + bottom half-ellipse (rx = W/2, ry = 0.58H)
//   teeth    band from the top edge down to 0.20H, clipped to the outline
//   tongue   ellipse centre (0.50W, 0.93H), rx 0.22W, ry 0.31H, clipped to the outline
//
// Every part is an animatable Shape parameterised by `ratio` (box height / width), so changing
// the ratio (mic level, saTalk keyframes, open <-> ajar) animates the whole mouth together.
// The container always has the OPEN aspect (170:130); the box is centred vertically in it.

struct MouthGeometry {
    let box: CGRect

    init(in rect: CGRect, ratio: CGFloat) {
        let width = rect.width
        let height = max(width * ratio, 0.5)
        box = CGRect(x: rect.minX, y: rect.midY - height / 2, width: width, height: height)
    }

    /// Bezier constant for a quarter ellipse.
    private static let kappa: CGFloat = 0.552_284_749_8

    var outline: Path {
        let k = Self.kappa
        let rx = box.width / 2
        let cx = box.midX
        let seamY = box.minY + Theme.Mouth.seam * box.height
        let top = Theme.Mouth.seam * box.height
        let bottom = (1 - Theme.Mouth.seam) * box.height
        var p = Path()
        p.move(to: CGPoint(x: cx - rx, y: seamY))
        p.addCurve(
            to: CGPoint(x: cx, y: seamY - top),
            control1: CGPoint(x: cx - rx, y: seamY - k * top),
            control2: CGPoint(x: cx - k * rx, y: seamY - top)
        )
        p.addCurve(
            to: CGPoint(x: cx + rx, y: seamY),
            control1: CGPoint(x: cx + k * rx, y: seamY - top),
            control2: CGPoint(x: cx + rx, y: seamY - k * top)
        )
        p.addCurve(
            to: CGPoint(x: cx, y: seamY + bottom),
            control1: CGPoint(x: cx + rx, y: seamY + k * bottom),
            control2: CGPoint(x: cx + k * rx, y: seamY + bottom)
        )
        p.addCurve(
            to: CGPoint(x: cx - rx, y: seamY),
            control1: CGPoint(x: cx - k * rx, y: seamY + bottom),
            control2: CGPoint(x: cx - rx, y: seamY + k * bottom)
        )
        p.closeSubpath()
        return p
    }

    var teeth: Path {
        Path(CGRect(x: box.minX, y: box.minY, width: box.width, height: box.height * Theme.Mouth.teeth))
    }

    var tongue: Path {
        let rx = Theme.Mouth.tongueRadiusX * box.width
        let ry = Theme.Mouth.tongueRadiusY * box.height
        let cy = box.minY + Theme.Mouth.tongueCenterY * box.height
        return Path(ellipseIn: CGRect(x: box.midX - rx, y: cy - ry, width: rx * 2, height: ry * 2))
    }
}

struct MouthOutlineShape: Shape {
    var ratio: CGFloat
    var animatableData: CGFloat {
        get { ratio }
        set { ratio = newValue }
    }

    func path(in rect: CGRect) -> Path { MouthGeometry(in: rect, ratio: ratio).outline }
}

struct MouthTeethShape: Shape {
    var ratio: CGFloat
    var animatableData: CGFloat {
        get { ratio }
        set { ratio = newValue }
    }

    func path(in rect: CGRect) -> Path { MouthGeometry(in: rect, ratio: ratio).teeth }
}

struct MouthTongueShape: Shape {
    var ratio: CGFloat
    var animatableData: CGFloat {
        get { ratio }
        set { ratio = newValue }
    }

    func path(in rect: CGRect) -> Path { MouthGeometry(in: rect, ratio: ratio).tongue }
}

/// The mouth mark. `ratio` is the box height / width: `Theme.Mouth.openRatio` (OPEN),
/// `Theme.Mouth.ajarRatio` (AJAR), anything in between while talking. `shut` draws the pill.
struct MouthView: View {
    var ratio: CGFloat = Theme.Mouth.openRatio
    var tongue: Bool = true
    var shut: Bool = false
    var style: Theme.MouthStyle = .standard

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            ZStack {
                if shut {
                    Capsule()
                        .fill(style.throat)
                        .frame(width: width * Theme.Mouth.shutWidth, height: width * Theme.Mouth.shutHeight)
                        .transition(.scale(scale: 0.6).combined(with: .opacity))
                } else {
                    ZStack {
                        MouthOutlineShape(ratio: ratio).fill(style.throat)
                        MouthTeethShape(ratio: ratio).fill(style.teeth)
                        if tongue {
                            MouthTongueShape(ratio: ratio).fill(style.tongue)
                                .transition(.opacity)
                        }
                    }
                    .clipShape(MouthOutlineShape(ratio: ratio))
                    .transition(.scale(scale: 0.8).combined(with: .opacity))
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .aspectRatio(1 / Theme.Mouth.openRatio, contentMode: .fit)
        .accessibilityHidden(true)
    }
}

/// saTalk: the mouth "talking" on a 1.6 s loop (brand keyframes). Used while the PC works.
struct TalkingMouthView: View {
    var style: Theme.MouthStyle = .standard
    var speed: Double = 1

    var body: some View {
        if MotionPolicy.frozen {
            MouthView(ratio: Theme.Mouth.openRatio * Theme.Motion.talkScale(5), tongue: true, style: style)
        } else {
            talking
        }
    }

    private var talking: some View {
        let style = self.style
        let s = max(speed, 0.1)
        return KeyframeAnimator(initialValue: CGFloat(1), repeating: true) { scale in
            MouthView(ratio: Theme.Mouth.openRatio * scale, tongue: true, style: style)
        } keyframes: { _ in
            KeyframeTrack {
                CubicKeyframe(Theme.Motion.talkScale(1), duration: Theme.Motion.talkStep(1) / s)
                CubicKeyframe(Theme.Motion.talkScale(2), duration: Theme.Motion.talkStep(2) / s)
                CubicKeyframe(Theme.Motion.talkScale(3), duration: Theme.Motion.talkStep(3) / s)
                CubicKeyframe(Theme.Motion.talkScale(4), duration: Theme.Motion.talkStep(4) / s)
                CubicKeyframe(Theme.Motion.talkScale(5), duration: Theme.Motion.talkStep(5) / s)
                CubicKeyframe(Theme.Motion.talkScale(6), duration: Theme.Motion.talkStep(6) / s)
                CubicKeyframe(Theme.Motion.talkScale(7), duration: Theme.Motion.talkStep(7) / s)
                CubicKeyframe(Theme.Motion.talkScale(8), duration: Theme.Motion.talkStep(8) / s)
                CubicKeyframe(Theme.Motion.talkScale(9), duration: Theme.Motion.talkStep(9) / s)
            }
        }
    }
}

/// saPunch values (scale + rotation).
struct PunchValues: Sendable {
    var scale: CGFloat = 1
    var angle: Double = 0
}

/// saPunch, once per change of `trigger`: a quick scale-up with a twist, then a rebound.
/// (The brand loop rests 0-38%, punches 38-60%; this plays just the punch part.)
/// A concrete modifier (Int trigger) rather than a generic View extension, so the animator's
/// @Sendable content closure doesn't capture a generic `Self.Type`.
struct ShoutPunchModifier: ViewModifier {
    var trigger: Int
    var strength: CGFloat

    func body(content: Content) -> some View {
        let strength = self.strength
        return content.keyframeAnimator(initialValue: PunchValues(), trigger: trigger) { view, value in
            view
                .scaleEffect(value.scale)
                .rotationEffect(.degrees(value.angle))
        } keyframes: { _ in
            let unit = Theme.Motion.punchDuration / 100
            KeyframeTrack(\.scale) {
                CubicKeyframe(1 + (Theme.Motion.punchPeakScale - 1) * strength, duration: 6 * unit)
                CubicKeyframe(1 - (1 - Theme.Motion.punchReboundScale) * strength, duration: 6 * unit)
                CubicKeyframe(1, duration: 10 * unit)
            }
            KeyframeTrack(\.angle) {
                CubicKeyframe(Theme.Motion.punchPeakAngle * Double(strength), duration: 6 * unit)
                CubicKeyframe(Theme.Motion.punchReboundAngle * Double(strength), duration: 6 * unit)
                CubicKeyframe(0, duration: 10 * unit)
            }
        }
    }
}

/// A short horizontal shake, once per change of `trigger` (errors).
struct ShakeModifier: ViewModifier {
    var trigger: Int

    func body(content: Content) -> some View {
        content.keyframeAnimator(initialValue: CGFloat(0), trigger: trigger) { view, x in
            view.offset(x: x)
        } keyframes: { _ in
            KeyframeTrack {
                LinearKeyframe(-14, duration: 0.06)
                LinearKeyframe(12, duration: 0.08)
                LinearKeyframe(-8, duration: 0.08)
                LinearKeyframe(5, duration: 0.07)
                SpringKeyframe(0, duration: 0.2)
            }
        }
    }
}

extension View {
    func shoutPunch(trigger: Int, strength: CGFloat = 1) -> some View {
        modifier(ShoutPunchModifier(trigger: trigger, strength: strength))
    }

    func shake(trigger: Int) -> some View {
        modifier(ShakeModifier(trigger: trigger))
    }
}
