import XCTest

/// The "done" zip: geometry and timeline (the same numbers as the desktop pill's).
final class ZipTimelineTests: XCTestCase {
    func testGeometryMatchesTheDesktop() {
        XCTAssertEqual(ZipGeometry.mid, 54.6, accuracy: 1e-9)
        XCTAssertEqual(ZipGeometry.from, -112)
        XCTAssertEqual(ZipGeometry.to, 176)
        XCTAssertEqual(ZipTimeline.swingPerSpeed, 0.07 * 288 / 0.552, accuracy: 1e-9)
        let ink = ZipGeometry.sliderBody.spread(ZipGeometry.ink)
        XCTAssertEqual(ink, ZipBox(x0: -6, y0: -6, x1: 46, y1: 50, topLeft: 16, topRight: 24, bottomRight: 24, bottomLeft: 16))
        XCTAssertEqual(ink.width, 52)
        XCTAssertEqual(ink.height, 56)
    }

    func testSpanProgress() {
        let span = ZipSpan(start: 1, end: 3)
        XCTAssertEqual(span.progress(0), 0)
        XCTAssertEqual(span.progress(2), 0.5)
        XCTAssertEqual(span.progress(5), 1)
        XCTAssertEqual(ZipSpan(start: 2, end: 2).progress(1), 0)
        XCTAssertEqual(ZipSpan(start: 2, end: 2).progress(2), 1)
    }

    func testTeeth() {
        XCTAssertEqual(ZipGeometry.teeth(right: 0), [])
        XCTAssertEqual(ZipGeometry.teeth(right: 28), [])
        XCTAssertEqual(ZipGeometry.teeth(right: 30), [ZipSpan(start: 14, end: 16)])
        XCTAssertEqual(ZipGeometry.teeth(right: 50), [ZipSpan(start: 14, end: 26)])
        XCTAssertEqual(ZipGeometry.teeth(right: 60), [ZipSpan(start: 14, end: 26), ZipSpan(start: 36, end: 46)])
        let sealed = ZipGeometry.teeth(right: ZipGeometry.boxWidth)
        XCTAssertEqual(sealed.count, 7)
        XCTAssertEqual(sealed.first, ZipSpan(start: 14, end: 26))
        XCTAssertEqual(sealed.last, ZipSpan(start: 146, end: 156))
        XCTAssertEqual(ZipGeometry.teeth(right: .nan), [])
    }

    func testSeamPolygon() {
        let points = ZipGeometry.seamPolygon(z: 10)
        XCTAssertEqual(points.count, 5)
        XCTAssertEqual(points[0], ZipPoint(x: 10, y: ZipGeometry.mid))
        XCTAssertEqual(points[1], ZipPoint(x: 110, y: -14))
        XCTAssertEqual(points[2], ZipPoint(x: 340, y: -14))
        XCTAssertEqual(points[3], ZipPoint(x: 340, y: 150))
        XCTAssertEqual(points[4], ZipPoint(x: 110, y: 150))
    }

    func testEasings() {
        XCTAssertEqual(ZipTimeline.easeInOutQuad(0), 0)
        XCTAssertEqual(ZipTimeline.easeInOutQuad(0.5), 0.5)
        XCTAssertEqual(ZipTimeline.easeInOutQuad(1), 1)
        XCTAssertEqual(ZipTimeline.easeOutBack(0), 0, accuracy: 1e-12)
        XCTAssertEqual(ZipTimeline.easeOutBack(1), 1)
        XCTAssertGreaterThan(ZipTimeline.easeOutBack(0.8), 1)  // it overshoots: the pop
    }

    func testStartsWideOpenWithNoSlider() {
        let pose = ZipTimeline.pose(at: 0)
        XCTAssertEqual(pose.z, ZipGeometry.from)
        XCTAssertEqual(pose.slider, 0)
        XCTAssertEqual(pose.swing, 0)
        XCTAssertTrue(pose.showsOpenMouth)
        XCTAssertFalse(pose.showsSlider)
        XCTAssertEqual(pose.lineRight, 0)
        XCTAssertEqual(pose.teeth, [])
        XCTAssertFalse(pose.showsIdleMouth)
    }

    func testSliderPopsInWithAnOvershoot() {
        let peak = stride(from: 0.0, through: 0.1, by: 0.005).map { ZipTimeline.pose(at: $0).slider }.max() ?? 0
        XCTAssertGreaterThan(peak, 1)
        XCTAssertEqual(ZipTimeline.pose(at: 0.1).slider, 1)
        XCTAssertEqual(ZipTimeline.pose(at: 0.2).slider, 1)
    }

    func testZipsLeftToRight() {
        var last = -Double.infinity
        for t in stride(from: 0.0, through: 0.4, by: 0.01) {
            let z = ZipTimeline.pose(at: t).z
            XCTAssertGreaterThanOrEqual(z, last)
            last = z
        }
        let half = ZipTimeline.pose(at: 0.18)
        XCTAssertEqual(half.z, 32, accuracy: 1e-9)
        XCTAssertEqual(half.swing, ZipTimeline.swingMax)  // the pull trails at full lean
        XCTAssertTrue(half.showsOpenMouth)
        XCTAssertEqual(half.lineRight, 46, accuracy: 1e-9)
        XCTAssertEqual(half.sliderX, 28, accuracy: 1e-9)
    }

    func testSealed() {
        let pose = ZipTimeline.pose(at: ZipTimeline.zipSpan.end)
        XCTAssertEqual(pose.z, ZipGeometry.to)
        XCTAssertFalse(pose.showsOpenMouth)  // the tongue is gone once it's zipped
        XCTAssertTrue(pose.showsZipper)
        XCTAssertEqual(pose.lineRight, ZipGeometry.boxWidth)
        XCTAssertEqual(pose.teeth.count, 7)
        XCTAssertEqual(pose.sliderX, ZipGeometry.parkMax - 4)
        XCTAssertEqual(pose.swing, 0, accuracy: 1e-9)
    }

    func testSquashesOnceThenThePullSettles() {
        XCTAssertEqual(ZipTimeline.pose(at: 0.3).squash, 0)
        XCTAssertEqual(ZipTimeline.pose(at: 0.42).squash, 1, accuracy: 1e-9)
        XCTAssertEqual(ZipTimeline.pose(at: 0.52).squash, 0)
        let squashed = ZipTimeline.pose(at: 0.42)
        XCTAssertEqual(squashed.scaleX, 1.08, accuracy: 1e-9)
        XCTAssertEqual(squashed.scaleY, 0.85, accuracy: 1e-9)
        XCTAssertGreaterThan(abs(ZipTimeline.pose(at: 0.36).swing), 5)
        XCTAssertLessThan(abs(ZipTimeline.pose(at: 0.6).swing), 1)
    }

    func testFlattensAwayAndTheIdleMouthComesBack() {
        let flattening = ZipTimeline.pose(at: 0.55)
        XCTAssertGreaterThan(flattening.flat, 0)
        XCTAssertLessThan(flattening.flat, 1)
        XCTAssertTrue(flattening.showsZipper)
        XCTAssertFalse(flattening.showsIdleMouth)

        let gone = ZipTimeline.pose(at: ZipTimeline.flatSpan.end)
        XCTAssertFalse(gone.showsZipper)
        XCTAssertFalse(gone.showsSlider)
        XCTAssertFalse(gone.showsOpenMouth)

        let popping = ZipTimeline.pose(at: 0.66)
        XCTAssertTrue(popping.showsIdleMouth)
        XCTAssertGreaterThan(popping.idleScale, ZipTimeline.idleFromScale)

        let end = ZipTimeline.pose(at: ZipTimeline.duration)
        XCTAssertEqual(end.idleScale, 1, accuracy: 1e-9)
        XCTAssertEqual(end.idleOpacity, 1)
        XCTAssertFalse(end.showsZipper)
        XCTAssertEqual(ZipTimeline.pose(at: 10), end)
        XCTAssertEqual(ZipTimeline.pose(at: .nan), end)
        XCTAssertEqual(ZipTimeline.pose(at: -1), ZipTimeline.pose(at: 0))
    }

    func testIsQuick() {
        XCTAssertLessThanOrEqual(ZipTimeline.duration, 0.75)
        XCTAssertEqual(ZipTimeline.length(reduceMotion: false), ZipTimeline.duration)
        XCTAssertLessThan(ZipTimeline.length(reduceMotion: true), ZipTimeline.duration)
    }

    func testReduceMotionFadesTheSealedZipperIntoTheIdleMouth() {
        let first = ZipTimeline.pose(at: 0, reduceMotion: true)
        XCTAssertEqual(first.z, ZipGeometry.to)
        XCTAssertEqual(first.slider, 1)
        XCTAssertEqual(first.swing, 0)
        XCTAssertEqual(first.opacity, 1)
        XCTAssertFalse(first.showsOpenMouth)
        XCTAssertFalse(first.showsIdleMouth)

        let middle = ZipTimeline.pose(at: 0.35, reduceMotion: true)
        XCTAssertEqual(middle.opacity, 0.5, accuracy: 1e-9)
        XCTAssertEqual(middle.idleOpacity, 0.5, accuracy: 1e-9)
        XCTAssertEqual(middle.idleScale, 1)

        let end = ZipTimeline.pose(at: ZipTimeline.reducedDuration, reduceMotion: true)
        XCTAssertEqual(end.opacity, 0)
        XCTAssertFalse(end.showsZipper)
        XCTAssertEqual(end.idleOpacity, 1)
    }

    func testIslandStills() {
        XCTAssertEqual(ZipTimeline.still(progress: 0), .start)
        XCTAssertEqual(ZipTimeline.still(progress: 1), .sealed)
        XCTAssertEqual(ZipTimeline.still(progress: -3), .start)
        XCTAssertEqual(ZipTimeline.still(progress: 7), .sealed)
        XCTAssertEqual(ZipTimeline.still(progress: .nan), .sealed)
        let half = ZipTimeline.still(progress: 0.5)
        XCTAssertEqual(half.swing, ZipTimeline.swingMax)
        XCTAssertTrue(half.showsOpenMouth)
        XCTAssertTrue(half.showsSlider)
        XCTAssertTrue(ZipPose.start.showsOpenMouth)
        XCTAssertTrue(ZipPose.start.showsSlider)
        XCTAssertFalse(ZipPose.sealed.showsOpenMouth)
        XCTAssertTrue(ZipPose.sealed.showsSlider)
    }
}

/// The zip in the Live Activity's state.
final class DictationActivityZipTests: XCTestCase {
    private func done(_ outcome: DictationActivityState.Outcome?) -> DictationActivityState {
        var state = DictationActivityState(phase: .done, target: .pc, computerName: "Sample-Desktop", startedAt: Date(timeIntervalSince1970: 1_000))
        state.outcome = outcome
        return state
    }

    func testOnlyDeliveredDictationsZip() {
        XCTAssertTrue(done(.pasted).zips)
        XCTAssertTrue(done(.copied).zips)
        XCTAssertFalse(done(.nothing).zips)
        XCTAssertFalse(done(nil).zips)
        var sending = done(.pasted)
        sending.phase = .sending
        XCTAssertFalse(sending.zips)
        var failed = done(.pasted)
        failed.phase = .failed
        XCTAssertFalse(failed.zips)
    }

    func testZipProgress() {
        var state = done(.pasted)
        XCTAssertNil(state.zip)
        XCTAssertEqual(state.zipProgress, 1)  // nil = sealed
        state.zip = 0
        XCTAssertEqual(state.zipProgress, 0)
        state.zip = 0.5
        XCTAssertEqual(state.zipProgress, 0.5)
        state.zip = 4
        XCTAssertEqual(state.zipProgress, 1)
        state.zip = -1
        XCTAssertEqual(state.zipProgress, 0)
        state.zip = .infinity
        XCTAssertEqual(state.zipProgress, 1)
    }

    func testNoCheckMarks() {
        for outcome in [DictationActivityState.Outcome.pasted, .copied, .nothing] {
            let state = done(outcome)
            XCTAssertFalse(state.badge.contains("✓"))
            XCTAssertFalse(state.doneSymbol.contains("checkmark"))
            XCTAssertFalse(DictationActivityState.doneDetail(outcome, notice: nil, computerName: "Sample-Desktop").contains("✓"))
        }
        XCTAssertFalse(done(nil).badge.contains("✓"))
        XCTAssertEqual(done(.pasted).doneSymbol, "desktopcomputer")
        XCTAssertEqual(done(.copied).doneSymbol, "doc.on.clipboard")
        XCTAssertEqual(done(nil).doneSymbol, DictationTarget.pc.systemImage)
    }

    func testZipIsOptionalInThePayload() throws {
        // A payload from before the zip (no "zip" key) still decodes, as sealed.
        let old = done(.pasted)
        let encoded = try JSONEncoder().encode(old)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertNil(object["zip"])
        let decoded = try JSONDecoder().decode(DictationActivityState.self, from: encoded)
        XCTAssertNil(decoded.zip)
        XCTAssertEqual(decoded.zipProgress, 1)

        var zipping = old
        zipping.zip = 0.5
        let roundTrip = try JSONDecoder().decode(DictationActivityState.self, from: JSONEncoder().encode(zipping))
        XCTAssertEqual(roundTrip, zipping)
        XCTAssertEqual(roundTrip.zip, 0.5)
    }
}
