import Foundation

// The "done" finish: the mouth zips shut. The owner's "Paddle pull" design (zip variations v2,
// 2c), with the same geometry and timeline as the desktop pill (src/shoutabout/overlay.py), in
// the open mouth's 170 x 130 reference box (y down):
//
//   a white slider pops in at the left and zips along the mouth's widest line; behind it a V
//   seam closes the mouth into an ink line with white zipper teeth (the tongue goes only as the
//   zip covers it); a paddle pull hangs from the slider and swings with it. Sealed, the line
//   squashes once while the pull settles, then everything flattens away. The desktop then fades
//   its pill; on the iPhone the idle mouth pops back in.
//
// Pure Foundation (unit-tested). Shared/UI/ZipMouth.swift draws a `ZipPose`.

/// A stretch of the zip: seconds on its timeline, or box px along the zipped line.
struct ZipSpan: Equatable, Sendable {
    var start: Double
    var end: Double

    /// 0 up to `start`, 1 from `end`, linear between.
    func progress(_ t: Double) -> Double {
        guard end > start else { return t >= end ? 1 : 0 }
        return min(1, max(0, (t - start) / (end - start)))
    }
}

/// A point in the mouth's box (reference px, y down).
struct ZipPoint: Equatable, Sendable {
    var x: Double
    var y: Double
}

/// A rectangle with its own radius at each corner (like CSS `border-radius`), in the slider's
/// own 40 x 44 box.
struct ZipBox: Equatable, Sendable {
    var x0: Double
    var y0: Double
    var x1: Double
    var y1: Double
    var topLeft: Double
    var topRight: Double
    var bottomRight: Double
    var bottomLeft: Double

    var width: Double { x1 - x0 }
    var height: Double { y1 - y0 }

    /// The ink outline under a white part: the box and every radius grow by `d` (a CSS
    /// box-shadow spread).
    func spread(_ d: Double) -> ZipBox {
        ZipBox(
            x0: x0 - d, y0: y0 - d, x1: x1 + d, y1: y1 + d,
            topLeft: topLeft + d, topRight: topRight + d, bottomRight: bottomRight + d, bottomLeft: bottomLeft + d
        )
    }
}

/// The zip's geometry, in the open mouth's box (170 x 130 reference px, y down).
enum ZipGeometry {
    static let boxWidth: Double = 170
    static let boxHeight: Double = 130
    /// The mouth's widest line (where its top and bottom half-ellipses meet): it zips here.
    static let mid: Double = 0.42 * boxHeight
    /// The V seam reaches the box's top and bottom this far right of its point.
    static let seam: Double = 100
    /// The seam's point travels from / to here...
    static let from: Double = -seam - 12
    static let to: Double = 176
    /// ...and the slider follows it between these.
    static let parkMin: Double = -14
    static let parkMax: Double = 128
    /// The zipped line's height.
    static let lip: Double = 34
    /// Its white teeth: width, gap, height, and inset from the line's ends.
    static let tooth: Double = 12
    static let toothGap: Double = 10
    static let toothHeight: Double = 12
    static let toothInset: Double = 14
    /// The slider's and the pull's ink outline.
    static let ink: Double = 6
    /// Sealed, the line squashes once: this much wider, this much flatter.
    static let squashWide: Double = 0.08
    static let squashFlat: Double = 0.15

    /// The slider's box, and its parts in it (from the design's CSS). The pull hangs below
    /// the slider and swings about the slider's centre; the slider is painted over it.
    static let sliderWidth: Double = 40
    static let sliderHeight: Double = 44
    static let sliderBody = ZipBox(x0: 0, y0: 0, x1: 40, y1: 44, topLeft: 10, topRight: 18, bottomRight: 18, bottomLeft: 10)
    static let pullBody = ZipBox(x0: 0, y0: 38, x1: 40, y1: 84, topLeft: 8, topRight: 8, bottomRight: 20, bottomLeft: 20)
    /// An ink slot in the pull.
    static let pullSlot = ZipBox(x0: 12, y0: 58, x1: 28, y1: 66, topLeft: 4, topRight: 4, bottomRight: 4, bottomLeft: 4)

    /// The open mouth still shows inside this polygon: right of a V whose point is at `z` on
    /// the zip line and whose arms reach the box's top and bottom `seam` px further right.
    static func seamPolygon(z: Double) -> [ZipPoint] {
        let far = 2 * boxWidth
        return [
            ZipPoint(x: z, y: mid),
            ZipPoint(x: z + seam, y: -14),
            ZipPoint(x: far, y: -14),
            ZipPoint(x: far, y: boxHeight + 20),
            ZipPoint(x: z + seam, y: boxHeight + 20),
        ]
    }

    /// The white teeth on a zipped line that ends at `right` (it starts at the box's left
    /// edge): `tooth` wide, `toothGap` apart, `toothInset` in from both ends.
    static func teeth(right: Double) -> [ZipSpan] {
        guard right.isFinite else { return [] }
        var spans: [ZipSpan] = []
        var x = toothInset
        while x < right - toothInset {
            spans.append(ZipSpan(start: x, end: min(x + tooth, right - toothInset)))
            x += tooth + toothGap
        }
        return spans
    }
}

/// One moment of the zip.
struct ZipPose: Equatable, Sendable {
    /// The V seam's point (`ZipGeometry.from ... to`): the mouth is zipped left of it.
    var z: Double
    /// The slider's (and its pull's) scale: it pops in.
    var slider: Double = 1
    /// The pull's angle in degrees (+ = clockwise on screen: trailing a move to the right).
    var swing: Double = 0
    /// Sealed, the line squashes once, 0...1.
    var squash: Double = 0
    /// The zipped line, slider and pull flattening away, 0...1 (1 = gone).
    var flat: Double = 0
    /// The whole zip's opacity (Reduce Motion fades it instead).
    var opacity: Double = 1
    /// The idle mouth coming back after it (0 opacity = not there yet).
    var idleScale: Double = 0
    var idleOpacity: Double = 0

    /// The slider has just popped in at the left of the wide-open mouth.
    static let start = ZipPose(z: ZipGeometry.from)
    /// Zipped shut, the pull hanging still.
    static let sealed = ZipPose(z: ZipGeometry.to)

    /// What's left of the open mouth (right of the seam, tongue and all) still shows.
    var showsOpenMouth: Bool { opacity > 0 && z < ZipGeometry.boxWidth && squash == 0 && flat == 0 }
    /// The zipped line, its teeth, the slider and the pull still show.
    var showsZipper: Bool { opacity > 0 && flat < 1 && scaleY > 0 }
    var showsSlider: Bool { showsZipper && slider > 0.01 }
    var showsIdleMouth: Bool { idleOpacity > 0 }

    /// The zipped line's right end (it starts at the box's left edge).
    var lineRight: Double { min(max(z + ZipGeometry.toothInset, 0), ZipGeometry.boxWidth) }
    var teeth: [ZipSpan] { ZipGeometry.teeth(right: lineRight) }
    /// The slider box's top-left corner.
    var sliderX: Double { min(max(z, ZipGeometry.parkMin), ZipGeometry.parkMax) - 4 }
    var sliderY: Double { ZipGeometry.mid - ZipGeometry.sliderHeight / 2 }
    /// Squash and flatten, about the zip line (the box's centre horizontally).
    var scaleX: Double { 1 + ZipGeometry.squashWide * squash }
    var scaleY: Double { max(0, (1 - ZipGeometry.squashFlat * squash) * (1 - flat)) }
}

/// When things happen, in seconds after the dictation landed.
enum ZipTimeline {
    /// The slider pops in (ease-out-back).
    static let popSpan = ZipSpan(start: 0, end: 0.1)
    /// It zips left to right (ease-in-out).
    static let zipSpan = ZipSpan(start: 0.03, end: 0.33)
    /// Sealed: the line squashes once.
    static let squashSpan = ZipSpan(start: 0.33, end: 0.51)
    /// The zipped line, slider and pull flatten away (ease-in: sucked flat).
    static let flatSpan = ZipSpan(start: 0.47, end: 0.63)
    /// The idle mouth pops back in (where the desktop fades its pill).
    static let idleSpan = ZipSpan(start: 0.6, end: 0.73)
    static let duration: Double = 0.73

    /// Reduce Motion: no zipping; the sealed zipper shows at once and fades into the idle mouth.
    static let reducedFadeSpan = ZipSpan(start: 0.25, end: 0.45)
    static let reducedDuration: Double = 0.45

    /// The design swings the pull 0.07° per box px/s of its 0.552 s zip; per bit of progress
    /// instead, so the swing looks the same at this (faster) speed.
    static let swingPerSpeed: Double = 0.07 * (ZipGeometry.to - ZipGeometry.from) / 0.552
    static let swingMax: Double = 40
    /// Sealed, the pull swings on and settles: amplitude (degrees), decay (/s), rate (rad/s).
    static let settleAmplitude: Double = 26
    static let settleDecay: Double = 14
    static let settleRate: Double = 40
    /// The idle mouth grows back from this scale.
    static let idleFromScale: Double = 0.6

    /// How long it plays.
    static func length(reduceMotion: Bool) -> Double {
        reduceMotion ? reducedDuration : duration
    }

    /// Where the zip is `time` seconds after the dictation landed. Pure.
    static func pose(at time: Double, reduceMotion: Bool = false) -> ZipPose {
        let t = time.isFinite ? max(0, time) : length(reduceMotion: reduceMotion)
        if reduceMotion {
            let fade = reducedFadeSpan.progress(t)
            var pose = ZipPose.sealed
            pose.opacity = 1 - fade
            pose.idleScale = 1
            pose.idleOpacity = fade
            return pose
        }
        let u = zipSpan.progress(t)
        var pose = ZipPose(z: seamPoint(progress: u))
        pose.slider = t > popSpan.start ? easeOutBack(popSpan.progress(t)) : 0
        var angle = swing(progress: u)
        let sinceSealed = t - zipSpan.end
        if sinceSealed > 0 {
            angle += settleAmplitude * exp(-settleDecay * sinceSealed) * sin(settleRate * sinceSealed)
        }
        pose.swing = angle
        if t > squashSpan.start, t < squashSpan.end {
            pose.squash = sin(Double.pi * squashSpan.progress(t))
        }
        let flatness = flatSpan.progress(t)
        pose.flat = flatness * flatness
        if pose.flat >= 1 {  // gone: nothing of the zip is left
            pose = ZipPose(z: ZipGeometry.to, slider: 0, flat: 1)
        }
        let back = idleSpan.progress(t)
        if back > 0 {
            pose.idleScale = idleFromScale + (1 - idleFromScale) * easeOutBack(back)
            pose.idleOpacity = min(1, 2 * back)
        }
        return pose
    }

    /// A still along the zip for the Live Activity (which can't run animations: the island
    /// animates between the stills the app sends). 0 = the slider has just popped in at the
    /// left, 1 = sealed; the pull leans with the slider's speed.
    static func still(progress: Double) -> ZipPose {
        let u = progress.isFinite ? min(max(progress, 0), 1) : 1
        return ZipPose(z: seamPoint(progress: u), swing: swing(progress: u))
    }

    /// The seam's point at zip progress `u` (0...1).
    static func seamPoint(progress u: Double) -> Double {
        ZipGeometry.from + (ZipGeometry.to - ZipGeometry.from) * easeInOutQuad(u)
    }

    /// The pull trails the slider, leaning with its speed (the ease's slope: 0 at both ends).
    static func swing(progress u: Double) -> Double {
        let speed = u < 0.5 ? 4 * u : 4 * (1 - u)
        return min(swingMax, swingPerSpeed * max(0, speed))
    }

    static func easeInOutQuad(_ u: Double) -> Double {
        u < 0.5 ? 2 * u * u : -1 + (4 - 2 * u) * u
    }

    static func easeOutBack(_ u: Double) -> Double {
        let c1 = 1.70158
        let c3 = c1 + 1
        let v = u - 1
        return 1 + c3 * v * v * v + c1 * v * v
    }
}
