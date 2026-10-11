import SwiftUI

// The mouth zipping shut: the "done" finish (the owner's "Paddle pull" design, the same geometry
// and timing as the desktop pill). A pure function of a `ZipPose` (Shared/Core/ZipTimeline.swift),
// drawn in the open mouth's box with the brand colours, bottom to top:
//
//   what's left of the open mouth right of the V seam (teeth, throat and red tongue: the mouth is
//   never open without its tongue), the zipped ink line with its white teeth, then the paddle
//   pull and the white slider, each with an ink outline.
//
// Plain shapes with frames and offsets (no Canvas), so the Live Activity can draw it too and
// animate the line and the slider between the stills the app sends.

/// The zipping mouth. Same container as `MouthView` (the OPEN aspect, 170:130), so the two
/// can be swapped or stacked.
struct ZipMouthView: View {
    var pose: ZipPose
    var style: Theme.MouthStyle = .standard

    var body: some View {
        GeometryReader { geo in
            let scale = geo.size.width / CGFloat(ZipGeometry.boxWidth)
            ZStack(alignment: .topLeading) {
                if pose.showsOpenMouth {
                    MouthView(ratio: Theme.Mouth.openRatio, tongue: true, style: style)
                        .frame(width: geo.size.width, height: geo.size.height)
                        .clipShape(ZipSeamShape(z: pose.z))
                }
                if pose.showsZipper {
                    ZipperParts(pose: pose, style: style, scale: scale)
                        .scaleEffect(x: CGFloat(pose.scaleX), y: CGFloat(max(pose.scaleY, 0.001)), anchor: lineAnchor)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .topLeading)
        }
        .aspectRatio(CGFloat(ZipGeometry.boxWidth / ZipGeometry.boxHeight), contentMode: .fit)
        .opacity(pose.opacity)
        .accessibilityHidden(true)
    }

    /// Squash and flatten keep the zip line where it is.
    private var lineAnchor: UnitPoint {
        UnitPoint(x: 0.5, y: CGFloat(ZipGeometry.mid / ZipGeometry.boxHeight))
    }
}

/// The zipped line, its teeth, the pull and the slider, laid out in the mouth's box.
private struct ZipperParts: View {
    var pose: ZipPose
    var style: Theme.MouthStyle
    /// Points per box px.
    var scale: CGFloat

    var body: some View {
        ZStack(alignment: .topLeading) {
            Capsule()
                .fill(style.throat)
                .frame(width: px(pose.lineRight), height: px(ZipGeometry.lip))
                .offset(y: px(ZipGeometry.mid - ZipGeometry.lip / 2))
            ZipTeethShape(right: pose.lineRight)
                .fill(style.teeth)
                .frame(width: px(ZipGeometry.boxWidth), height: px(ZipGeometry.boxHeight))
            if pose.showsSlider {
                ZipSliderView(swing: pose.swing, style: style)
                    .frame(width: px(ZipGeometry.sliderWidth), height: px(ZipGeometry.sliderHeight))
                    .scaleEffect(CGFloat(pose.slider))
                    .offset(x: px(pose.sliderX), y: px(pose.sliderY))
            }
        }
        .frame(width: px(ZipGeometry.boxWidth), height: px(ZipGeometry.boxHeight), alignment: .topLeading)
    }

    private func px(_ value: Double) -> CGFloat {
        CGFloat(value) * scale
    }
}

/// The white slider with its paddle pull hanging below it; the pull swings about the slider's
/// centre (the centre of this view's frame, the slider's 40 x 44 box).
private struct ZipSliderView: View {
    var swing: Double
    var style: Theme.MouthStyle

    var body: some View {
        ZStack {
            ZStack {
                ZipBoxShape(box: ZipGeometry.pullBody.spread(ZipGeometry.ink)).fill(style.throat)
                ZipBoxShape(box: ZipGeometry.pullBody).fill(style.teeth)
                ZipBoxShape(box: ZipGeometry.pullSlot).fill(style.throat)
            }
            .rotationEffect(.degrees(swing))
            ZipBoxShape(box: ZipGeometry.sliderBody.spread(ZipGeometry.ink)).fill(style.throat)
            ZipBoxShape(box: ZipGeometry.sliderBody).fill(style.teeth)
        }
    }
}

/// Everything right of the V seam (the open mouth is clipped to it). `rect` is the mouth's box.
struct ZipSeamShape: Shape {
    var z: Double

    func path(in rect: CGRect) -> Path {
        let scale = rect.width / CGFloat(ZipGeometry.boxWidth)
        let points = ZipGeometry.seamPolygon(z: z).map { point in
            CGPoint(x: rect.minX + CGFloat(point.x) * scale, y: rect.minY + CGFloat(point.y) * scale)
        }
        var path = Path()
        path.addLines(points)
        path.closeSubpath()
        return path
    }
}

/// The zipped line's white teeth up to `right`. `rect` is the mouth's box.
struct ZipTeethShape: Shape {
    var right: Double

    func path(in rect: CGRect) -> Path {
        let scale = rect.width / CGFloat(ZipGeometry.boxWidth)
        let top = rect.minY + CGFloat(ZipGeometry.mid - ZipGeometry.toothHeight / 2) * scale
        let height = CGFloat(ZipGeometry.toothHeight) * scale
        var path = Path()
        for span in ZipGeometry.teeth(right: right) {
            path.addRect(CGRect(
                x: rect.minX + CGFloat(span.start) * scale,
                y: top,
                width: CGFloat(span.end - span.start) * scale,
                height: height
            ))
        }
        return path
    }
}

/// One part of the slider or its pull. `rect` is the slider's 40 x 44 box (parts may reach
/// outside it: the pull hangs below).
struct ZipBoxShape: Shape {
    var box: ZipBox

    func path(in rect: CGRect) -> Path {
        let scale = rect.width / CGFloat(ZipGeometry.sliderWidth)
        let frame = CGRect(
            x: rect.minX + CGFloat(box.x0) * scale,
            y: rect.minY + CGFloat(box.y0) * scale,
            width: CGFloat(box.width) * scale,
            height: CGFloat(box.height) * scale
        )
        return ZipPaths.roundedRect(
            frame,
            topLeft: CGFloat(box.topLeft) * scale,
            topRight: CGFloat(box.topRight) * scale,
            bottomRight: CGFloat(box.bottomRight) * scale,
            bottomLeft: CGFloat(box.bottomLeft) * scale
        )
    }
}

enum ZipPaths {
    /// Bezier constant for a quarter ellipse.
    private static let kappa: CGFloat = 0.552_284_749_8

    /// A rectangle with its own radius at each corner (each at most half the shorter side),
    /// clockwise on screen from the top-left.
    static func roundedRect(
        _ r: CGRect,
        topLeft: CGFloat,
        topRight: CGFloat,
        bottomRight: CGFloat,
        bottomLeft: CGFloat
    ) -> Path {
        let limit = max(0, min(r.width, r.height) / 2)
        let tl = min(max(topLeft, 0), limit)
        let tr = min(max(topRight, 0), limit)
        let br = min(max(bottomRight, 0), limit)
        let bl = min(max(bottomLeft, 0), limit)
        let k = kappa
        var p = Path()
        p.move(to: CGPoint(x: r.minX + tl, y: r.minY))
        p.addLine(to: CGPoint(x: r.maxX - tr, y: r.minY))
        p.addCurve(
            to: CGPoint(x: r.maxX, y: r.minY + tr),
            control1: CGPoint(x: r.maxX - tr + k * tr, y: r.minY),
            control2: CGPoint(x: r.maxX, y: r.minY + tr - k * tr)
        )
        p.addLine(to: CGPoint(x: r.maxX, y: r.maxY - br))
        p.addCurve(
            to: CGPoint(x: r.maxX - br, y: r.maxY),
            control1: CGPoint(x: r.maxX, y: r.maxY - br + k * br),
            control2: CGPoint(x: r.maxX - br + k * br, y: r.maxY)
        )
        p.addLine(to: CGPoint(x: r.minX + bl, y: r.maxY))
        p.addCurve(
            to: CGPoint(x: r.minX, y: r.maxY - bl),
            control1: CGPoint(x: r.minX + bl - k * bl, y: r.maxY),
            control2: CGPoint(x: r.minX, y: r.maxY - bl + k * bl)
        )
        p.addLine(to: CGPoint(x: r.minX, y: r.minY + tl))
        p.addCurve(
            to: CGPoint(x: r.minX + tl, y: r.minY),
            control1: CGPoint(x: r.minX, y: r.minY + tl - k * tl),
            control2: CGPoint(x: r.minX + tl - k * tl, y: r.minY)
        )
        p.closeSubpath()
        return p
    }
}

/// The finish in the app: the mouth zips shut, flattens away and the idle mouth pops back in
/// (`ZipTimeline.pose(at:)`), one frame per display refresh.
struct ZipFinishFrame: View {
    var pose: ZipPose
    /// The idle mouth it ends on (ajar, no tongue: `BreathingMouth`'s first frame).
    var idleRatio: CGFloat = Theme.Mouth.ajarRatio
    /// The zipping mouth's size relative to the idle one.
    var zipScale: CGFloat = 1
    var style: Theme.MouthStyle = .standard

    var body: some View {
        ZStack {
            if pose.showsIdleMouth {
                MouthView(ratio: idleRatio, tongue: false, style: style)
                    .scaleEffect(CGFloat(pose.idleScale))
                    .opacity(pose.idleOpacity)
            }
            ZipMouthView(pose: pose, style: style)
                .scaleEffect(zipScale)
        }
    }
}
