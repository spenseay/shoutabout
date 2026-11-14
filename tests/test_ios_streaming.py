"""iPhone: transcribe while you talk (streaming), tap anywhere, Cancel + Undo, and the
phone-mode auto-copy (ios/).

Swift can't be compiled here (no Mac); these pin down what would otherwise only fail on
Codemagic (a Shared/Core file leaning on app-only code, an intent switch missing a case) and the
owner's decisions (the .m4a stays the safe copy, keyboard dictations never stream, the stream
only on the LAN, the mouth button kept for VoiceOver and the UI tests). The XCTests
(StreamingTests, KeyboardBridgeTests) cover the behaviour on Codemagic."""

from __future__ import annotations

import re
from pathlib import Path

IOS = Path(__file__).resolve().parents[1] / "ios"
CORE = IOS / "Shared" / "Core"


def code_only(text: str) -> str:
    """Swift without string literals and comments (good enough for finding type names)."""
    text = re.sub(r'#"(?:[^"]|"(?!#))*"#', '""', text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def read(*parts: str) -> str:
    return (IOS.joinpath(*parts)).read_text("utf-8")


DECLARATION = re.compile(r"\b(?:struct|enum|class|protocol|typealias|actor)\s+([A-Z]\w*)")
TOP_LEVEL_DECLARATION = re.compile(
    r"^(?:@\w+(?:\([^)\n]*\))?\s+)*(?:(?:public|internal|private|fileprivate|final|open)\s+)*"
    r"(?:struct|enum|class|protocol|typealias|actor)\s+([A-Z]\w*)",
    re.M,
)


def declared_anywhere() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in IOS.rglob("*.swift"):
        for name in TOP_LEVEL_DECLARATION.findall(code_only(path.read_text("utf-8"))):
            found.setdefault(name, set()).add(str(path.relative_to(IOS)))
    return found


def uses_outside(files: list[Path], compiled: list[Path]) -> list[str]:
    """Project types used in `files` but declared only outside `compiled`."""
    everywhere = declared_anywhere()
    declared_here = {
        name for path in compiled for name in DECLARATION.findall(code_only(path.read_text("utf-8")))
    }
    problems = []
    for path in files:
        code = code_only(path.read_text("utf-8"))
        for name in sorted(set(re.findall(r"\b[A-Z]\w*\b", code))):
            if name in everywhere and name not in declared_here:
                problems.append(f"{path.relative_to(IOS)} uses {name} from {sorted(everywhere[name])}")
    return problems


def function_body(source: str, signature: str) -> str:
    """The text of the function starting at `signature`, up to its closing brace."""
    start = source.index(signature)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced braces after {signature}")


# MARK: Targets


def test_shared_core_only_uses_shared_core_types():
    """The unit tests and the widget extension compile Shared/Core without the app."""
    core = sorted(CORE.glob("*.swift"))
    assert not uses_outside(core, core), "\n".join(uses_outside(core, core))


def test_unit_tests_only_use_types_they_compile():
    tests = sorted((IOS / "ShoutAboutTests").glob("*.swift"))
    compiled = tests + sorted(CORE.glob("*.swift"))
    assert not uses_outside(tests, compiled), "\n".join(uses_outside(tests, compiled))


def test_streaming_core_is_foundation_only():
    for name in ("StreamProtocol.swift", "StreamSession.swift"):
        imports = re.findall(r"^import (\w+)", read("Shared", "Core", name), re.M)
        assert imports == ["Foundation"], (name, imports)


# MARK: Streaming


def test_stream_wire_format_matches_the_spec():
    wire = read("Shared", "Core", "StreamProtocol.swift")
    for text in (
        '"stream/start"',
        '"stream/\\(id)/audio"',
        'name: "seq"',
        '"stream/\\(id)/finish"',
        '"stream/\\(id)"',
        '"sample_rate"',
        '"s16le"',
        "sampleRate = 16_000",
        '"stream_id"',
        '"max_chunk_bytes"',
        '"next_seq"',
        'contains("stream")',
    ):
        assert text in wire, text
    uploader = read("ShoutAbout", "Services", "StreamingUploader.swift")
    assert 'request.httpMethod = "DELETE"' in uploader
    assert '"application/octet-stream"' in uploader
    assert "extension LiveHubTransport: HubStreamTransport" in uploader


def test_the_recording_file_stays_the_safe_copy():
    recorder = code_only(read("ShoutAbout", "Services", "AudioRecorder.swift"))
    assert "Stream" not in recorder  # AVAudioRecorder is untouched by streaming
    model = code_only(read("ShoutAbout", "App", "AppModel.swift"))
    # The stream's engine stops before the recorder closes the audio session.
    for signature in ("func stopAndSend()", "func cancelRecording()"):
        body = function_body(model, signature)
        assert body.index("takeStream()") < body.index("recorder.stop()"), signature
    # A failed stream falls back to the router with the same job (the .m4a).
    process = function_body(model, "private func process(")
    assert process.index("stream.finish(") < process.index("router.run(")
    # Retry never reuses a stream.
    assert "kept.stream = nil" in function_body(model, "private func fail(")


def test_keyboard_dictations_never_stream():
    model = code_only(read("ShoutAbout", "App", "AppModel.swift"))
    assert "if !forKeyboard { startStreamIfUseful(target: target) }" in model
    assert "keyboardSession: recorder.isLiveInputRunning" in model
    policy = code_only(read("Shared", "Core", "StreamSession.swift"))
    assert "guard !keyboardSession, let info, info.acceptsStream, info.speed != .slow" in policy
    assert "return computer?.lanEndpoint" in policy  # the LAN only, never the tunnel


# MARK: Auto-copy from the background


def test_every_system_outcome_is_handled_by_the_dictate_intent():
    model = read("ShoutAbout", "App", "AppModel.swift")
    enum = function_body(model, "enum SystemOutcome")
    cases = re.findall(r"^\s*case (\w+)", code_only(enum), re.M)
    assert "copyWaits" in cases
    intents = code_only(read("Shared", "Intents", "DictationIntents.swift"))
    switch = function_body(intents, "switch await DictationLauncher.toggleFromSystem()")
    for case in cases:
        assert re.search(rf"\.{case}\b", switch), case


def test_intents_come_forward_to_copy():
    intents = code_only(read("Shared", "Intents", "DictationIntents.swift"))
    stop = function_body(intents, "struct StopDictationIntent")
    assert "supportedModes: IntentModes = [.background, .foreground(.dynamic)]" in stop
    for name in ("struct DictateIntent", "struct StopDictationIntent"):
        body = function_body(intents, name)
        assert "continueInForeground(nil, alwaysConfirm: false)" in body, name
        assert "DictationLauncher.copyWaitingText()" in body, name


def test_the_keyboard_copies_what_it_types():
    controller = code_only(read("ShoutAboutKeyboard", "KeyboardViewController.swift"))
    body = function_body(controller, "private func typeWaitingDictation()")
    assert "if self.hasFullAccess { UIPasteboard.general.string = handoff.text }" in body
    assert "UIPasteboard" not in function_body(controller, "private func typeAgain()")


# MARK: The main screen


def test_tap_anywhere_keeps_the_mouth_button():
    view = code_only(read("ShoutAbout", "Views", "MainView.swift"))
    assert ".onTapGesture { model.toggleRecording() }" in view
    source = read("ShoutAbout", "Views", "MainView.swift")
    for identifier in ('"mouthButton"', '"cancelButton"', '"undoButton"', '"statusTitle"'):
        assert f".accessibilityIdentifier({identifier})" in source, identifier
    assert '"Tap anywhere to send"' in source
    assert "Color(hex:" not in source and "Theme.Palette." not in source  # tokens only


def test_recording_tokens_for_light_and_dark():
    theme = read("Shared", "UI", "Theme.swift")
    for token in ("groundRecording", "inkMutedRecording", "recordingStroke"):
        assert re.search(rf"static let {token} = Color\(light: .+, dark: .+\)", theme), token
    view = read("ShoutAbout", "Views", "MainView.swift")
    assert "Theme.Colors.groundRecording" in view
    assert "Theme.Colors.recordingStroke" in view


def test_cancel_keeps_the_audio_for_undo():
    model = code_only(read("ShoutAbout", "App", "AppModel.swift"))
    assert "static let undoWindow: Duration = .seconds(8)" in model
    cancel = function_body(model, "func cancelRecording()")
    assert "recorder.cancel()" not in cancel
    assert "keepForUndo(" in cancel
    undo = function_body(model, "func undoCancel()")
    assert "run(makeJob(item.recording, target: item.target))" in undo
