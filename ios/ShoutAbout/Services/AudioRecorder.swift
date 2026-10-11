@preconcurrency import AVFoundation
import Foundation

/// A finished recording on disk.
struct Recording: Sendable, Equatable {
    var url: URL
    var duration: TimeInterval
}

/// Records AAC (.m4a) mono at 16 kHz / 32 kbps: Whisper resamples to 16 kHz mono anyway, and
/// ~240 KB per minute uploads quickly on cellular. Metering drives the talking mouth.
@MainActor
final class AudioRecorder {
    enum RecorderError: Error, LocalizedError {
        case couldNotStart

        var errorDescription: String? {
            "Couldn't start the microphone. Another app may be using it."
        }
    }

    /// The hub rejects audio over 10 minutes; stop a little before.
    static let maxDuration: TimeInterval = 595

    private var recorder: AVAudioRecorder?

    var isRecording: Bool { recorder?.isRecording ?? false }
    var currentTime: TimeInterval { recorder?.currentTime ?? 0 }

    enum Permission: Equatable, Sendable {
        case granted
        case denied
        case undetermined
    }

    static var permission: Permission {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return .granted
        case .undetermined: return .undetermined
        default: return .denied
        }
    }

    static func requestPermission() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return true
        case .denied:
            return false
        case .undetermined:
            return await AVAudioApplication.requestRecordPermission()
        @unknown default:
            return false
        }
    }

    /// Encoder settings to try, best first (a device that rejects 16 kHz AAC at 32 kbps gets
    /// the encoder's default bitrate, then 44.1 kHz).
    private static func settingsCandidates() -> [[String: Any]] {
        [
            [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 16_000.0,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 32_000,
                AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
            ],
            [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 16_000.0,
                AVNumberOfChannelsKey: 1,
            ],
            [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 44_100.0,
                AVNumberOfChannelsKey: 1,
            ],
        ]
    }

    func start() throws {
        let session = AVAudioSession.sharedInstance()
        // Mixable: a recording started from the Action Button runs while the app is in the
        // background, where iOS refuses a session that would interrupt other audio.
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .mixWithOthers])
        try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
        try session.setActive(true)

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("dictation-\(UUID().uuidString).m4a")
        for settings in Self.settingsCandidates() {
            guard let candidate = try? AVAudioRecorder(url: url, settings: settings) else { continue }
            candidate.isMeteringEnabled = true
            if candidate.prepareToRecord(), candidate.record() {
                recorder = candidate
                return
            }
            candidate.deleteRecording()
        }
        try? session.setActive(false, options: .notifyOthersOnDeactivation)
        throw RecorderError.couldNotStart
    }

    /// Stops and returns the file (nil if nothing was recording).
    func stop() -> Recording? {
        guard let recorder else { return nil }
        let duration = recorder.currentTime
        recorder.stop()
        self.recorder = nil
        deactivateSession()
        return Recording(url: recorder.url, duration: duration)
    }

    /// Stops and deletes the recording.
    func cancel() {
        guard let recorder else { return }
        recorder.stop()
        recorder.deleteRecording()
        self.recorder = nil
        deactivateSession()
    }

    /// Current input level, 0 (silence) ... 1 (loud), perceptually scaled.
    func level() -> Double {
        guard let recorder, recorder.isRecording else { return 0 }
        recorder.updateMeters()
        let decibels = Double(recorder.averagePower(forChannel: 0))
        let floor = -50.0
        guard decibels > floor else { return 0 }
        let linear = (decibels - floor) / -floor
        return min(1, max(0, pow(linear, 1.6)))
    }

    private func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
