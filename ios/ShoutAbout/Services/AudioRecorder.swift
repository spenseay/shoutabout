@preconcurrency import AVFoundation
import Foundation

/// A finished recording on disk.
struct Recording: Sendable, Equatable {
    var url: URL
    var duration: TimeInterval
}

/// Records AAC (.m4a) mono at 16 kHz / 32 kbps: Whisper resamples to 16 kHz mono anyway, and
/// ~240 KB per minute uploads quickly on cellular. Metering drives the talking mouth.
///
/// While a keyboard session is open (`startLiveInput`), the microphone input keeps running in
/// the background and a recording is cut from it instead (`LiveInput`): iOS lets an app in the
/// background go on using a running input, but not open one, and that's how a tap on the
/// ShoutAbout keyboard starts a dictation without leaving the app you're typing in.
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
    /// The keyboard session's always-on input (nil: no session).
    private var live: LiveInput?
    private var onLiveInputLost: (@MainActor () -> Void)?

    var isRecording: Bool { recorder?.isRecording ?? (live?.isCapturing ?? false) }
    var currentTime: TimeInterval { recorder?.currentTime ?? (live?.captureDuration ?? 0) }
    var isLiveInputRunning: Bool { live != nil }

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

    /// Mixable, so a recording started in the background (the Action Button) or a keyboard
    /// session doesn't stop other audio: iOS refuses a session that would interrupt it there.
    private static func activateSession() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .mixWithOthers])
        try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
        try session.setActive(true)
    }

    func start() throws {
        if let live {
            try live.beginCapture()
            return
        }
        try Self.activateSession()

        let url = Self.newRecordingURL()
        for settings in Self.settingsCandidates() {
            guard let candidate = try? AVAudioRecorder(url: url, settings: settings) else { continue }
            candidate.isMeteringEnabled = true
            if candidate.prepareToRecord(), candidate.record() {
                recorder = candidate
                return
            }
            candidate.deleteRecording()
        }
        deactivateSession()
        throw RecorderError.couldNotStart
    }

    /// Stops and returns the file (nil if nothing was recording).
    func stop() -> Recording? {
        if let live, live.isCapturing {
            return live.endCapture()
        }
        guard let recorder else { return nil }
        let duration = recorder.currentTime
        recorder.stop()
        self.recorder = nil
        deactivateSession()
        return Recording(url: recorder.url, duration: duration)
    }

    /// Stops and deletes the recording.
    func cancel() {
        if let live, live.isCapturing {
            live.cancelCapture()
            return
        }
        guard let recorder else { return }
        recorder.stop()
        recorder.deleteRecording()
        self.recorder = nil
        deactivateSession()
    }

    /// Current input level, 0 (silence) ... 1 (loud), perceptually scaled.
    func level() -> Double {
        if let live {
            return live.isCapturing ? Self.scaled(decibels: live.decibels) : 0
        }
        guard let recorder, recorder.isRecording else { return 0 }
        recorder.updateMeters()
        return Self.scaled(decibels: Double(recorder.averagePower(forChannel: 0)))
    }

    private static func scaled(decibels: Double) -> Double {
        let floor = -50.0
        guard decibels > floor else { return 0 }
        let linear = (decibels - floor) / -floor
        return min(1, max(0, pow(linear, 1.6)))
    }

    private static func newRecordingURL() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("dictation-\(UUID().uuidString).m4a")
    }

    private func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // MARK: Live input (the keyboard session)

    /// Opens the microphone and keeps it running, throwing the audio away, until
    /// `stopLiveInput`; meanwhile `start()` begins a recording at once, also in the background.
    /// `onLost` runs when iOS takes the input away (a call, another app recording, a route or
    /// media-services change): the owner sends what was said and closes the session.
    func startLiveInput(onLost: @escaping @MainActor () -> Void) throws {
        guard live == nil else { return }
        guard recorder == nil else { throw RecorderError.couldNotStart }  // a recording has the microphone
        try Self.activateSession()
        do {
            live = try LiveInput { [weak self] in
                self?.liveInputLost()
            }
        } catch {
            deactivateSession()
            throw RecorderError.couldNotStart
        }
        onLiveInputLost = onLost
    }

    /// Closes it (a recording still being cut from it is thrown away).
    func stopLiveInput() {
        guard let input = live else { return }
        input.stop()
        live = nil
        onLiveInputLost = nil
        deactivateSession()
    }

    private func liveInputLost() {
        guard live != nil else { return }
        let handler = onLiveInputLost
        handler?()
        stopLiveInput()
    }
}

/// The keyboard session's input: an AVAudioEngine tap that runs all the time and writes to a
/// file only between `beginCapture` and `endCapture` (.m4a, AAC, the input's own sample rate,
/// so nothing has to be converted on the way).
@MainActor
private final class LiveInput {
    private let engine: AVAudioEngine
    private let sink: LiveCaptureSink
    private let format: AVAudioFormat
    private var captureURL: URL?
    private var observers: [any NSObjectProtocol] = []

    init(onLost: @escaping @MainActor @Sendable () -> Void) throws {
        let engine = AVAudioEngine()
        let sink = LiveCaptureSink()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw AudioRecorder.RecorderError.couldNotStart }
        // @Sendable: the tap runs on the engine's own thread, never on the main actor.
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { @Sendable (buffer, _) in
            sink.receive(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw AudioRecorder.RecorderError.couldNotStart
        }
        self.engine = engine
        self.sink = sink
        self.format = format
        observers = Self.observeLoss(of: engine, onLost: onLost)
    }

    var isCapturing: Bool { captureURL != nil }
    var captureDuration: TimeInterval { isCapturing ? sink.duration : 0 }
    var decibels: Double { sink.decibels }

    func beginCapture() throws {
        guard captureURL == nil else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("dictation-\(UUID().uuidString).m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: Int(format.channelCount),
        ]
        let file: AVAudioFile
        do {
            file = try AVAudioFile(
                forWriting: url,
                settings: settings,
                commonFormat: format.commonFormat,
                interleaved: format.isInterleaved
            )
        } catch {
            throw AudioRecorder.RecorderError.couldNotStart
        }
        sink.begin(file, sampleRate: format.sampleRate)
        captureURL = url
    }

    /// Finishes the file and returns it.
    func endCapture() -> Recording? {
        guard let url = captureURL else { return nil }
        captureURL = nil
        let duration = sink.end()
        return Recording(url: url, duration: duration)
    }

    func cancelCapture() {
        guard let url = captureURL else { return }
        captureURL = nil
        _ = sink.end()
        try? FileManager.default.removeItem(at: url)
    }

    func stop() {
        cancelCapture()
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
        observers = []
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    /// Interruptions (a call, Siri, another app's recording), the engine stopping on a route
    /// change, and a media-services reset all end the input.
    private static func observeLoss(of engine: AVAudioEngine, onLost: @escaping @MainActor @Sendable () -> Void) -> [any NSObjectProtocol] {
        let center = NotificationCenter.default
        let interruption = center.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { note in
            let type = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard type == AVAudioSession.InterruptionType.began.rawValue else { return }
            MainActor.assumeIsolated { onLost() }
        }
        let configuration = center.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { _ in
            MainActor.assumeIsolated { onLost() }
        }
        let reset = center.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil,
            queue: .main
        ) { _ in
            MainActor.assumeIsolated { onLost() }
        }
        return [interruption, configuration, reset]
    }
}

/// Shared by the main actor and the engine's tap thread: a lock guards the file being written,
/// the frame count and the level.
private final class LiveCaptureSink: @unchecked Sendable {
    private let lock = NSLock()
    private var file: AVAudioFile?
    private var frames: AVAudioFramePosition = 0
    private var sampleRate: Double = 0
    private var level: Double = -160

    func begin(_ file: AVAudioFile, sampleRate: Double) {
        lock.lock()
        defer { lock.unlock() }
        self.file = file
        frames = 0
        self.sampleRate = sampleRate
    }

    /// Stops writing; the seconds written. AVAudioFile finishes the file when it's released,
    /// which is here.
    func end() -> TimeInterval {
        lock.lock()
        defer { lock.unlock() }
        file = nil
        return sampleRate > 0 ? Double(frames) / sampleRate : 0
    }

    var duration: TimeInterval {
        lock.lock()
        defer { lock.unlock() }
        return sampleRate > 0 ? Double(frames) / sampleRate : 0
    }

    /// RMS level of the latest buffer, dBFS.
    var decibels: Double {
        lock.lock()
        defer { lock.unlock() }
        return level
    }

    /// On the tap's thread: every buffer updates the level; between begin and end it's written.
    func receive(_ buffer: AVAudioPCMBuffer) {
        let db = Self.decibels(of: buffer)
        lock.lock()
        defer { lock.unlock() }
        level = db
        guard let file else { return }
        do {
            try file.write(from: buffer)
            frames += AVAudioFramePosition(buffer.frameLength)
        } catch {
            // A buffer the encoder refused is skipped; the rest of the recording still counts.
        }
    }

    private static func decibels(of buffer: AVAudioPCMBuffer) -> Double {
        guard let channels = buffer.floatChannelData, buffer.frameLength > 0 else { return -160 }
        let samples = channels[0]
        let count = Int(buffer.frameLength)
        var sum: Float = 0
        for index in 0..<count {
            let sample = samples[index]
            sum += sample * sample
        }
        let rms = (sum / Float(count)).squareRoot()
        return rms > 0 ? Double(20 * log10(rms)) : -160
    }
}
