"""macOS platform pieces with fakes: Bonjour command, LaunchAgent, afconvert decoding,
Apple Silicon detection and model tiers, config, self-test markers, the Tk copy filter,
permission menu lines, doctor rows, the icon file and the bundle rules."""

from __future__ import annotations

import plistlib
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from shoutabout import audio, hardware, modelpick
from shoutabout.config import Config, ConfigError, Paths, parse_config
from shoutabout.discovery import ServiceInfo
from shoutabout.mac import decode_mac, doctor_mac, keys, permissions, platform, sysinfo, tkmac
from shoutabout.mac.discovery_mac import MacAdvertiser, dns_sd_command, watchdog_command
from shoutabout.mac.menubar import EXTRA_BASE, applescript_string, menu_entries
from shoutabout.mac.startup_mac import MacStartupManager, agent_plist
from shoutabout.state import HubState
from shoutabout.tray import TrayActions

REPO = Path(__file__).resolve().parents[1]
INFO = ServiceInfo(
    "Sam's MacBook Pro",
    47821,
    (("v", "1"), ("id", "1234"), ("name", "Sam's MacBook Pro"), ("ver", "0.1.0")),
)

# ------------------------------------------------------------------ Bonjour


def test_dns_sd_command_has_the_v2_contract_and_no_shell():
    argv = dns_sd_command(INFO)
    assert argv[:6] == [
        "/usr/bin/dns-sd",
        "-R",
        "Sam's MacBook Pro",
        "_shoutabout._tcp",
        "local",
        "47821",
    ]
    assert argv[6:] == ["v=1", "id=1234", "name=Sam's MacBook Pro", "ver=0.1.0"]
    dog = watchdog_command(INFO)
    assert dog[:2] == ["/bin/sh", "-c"] and dog[4:] == argv  # arguments, not a shell string
    assert "read" in dog[2] and "kill" in dog[2]


class FakeProc:
    def __init__(self):
        self.stdin = SimpleNamespace(closed=False, close=self._close)
        self.done = False

    def _close(self):
        self.stdin.closed = True
        self.done = True

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout=None):
        return 0


def test_advertiser_starts_once_and_stops_by_closing_the_pipe():
    spawned = []

    def spawn(argv):
        spawned.append((argv, FakeProc()))
        return spawned[-1][1]

    adv = MacAdvertiser(spawn=spawn)
    assert adv.update(INFO) and adv.info == INFO
    assert adv.update(INFO) and len(spawned) == 1  # same info: no restart
    renamed = ServiceInfo("Office Mac", 47821, INFO.txt)
    assert adv.update(renamed) and len(spawned) == 2
    assert spawned[0][1].stdin.closed  # the old announcement was withdrawn
    adv.stop()
    assert spawned[1][1].stdin.closed and adv.info is None


def test_advertiser_without_dns_sd():
    def spawn(argv):
        raise FileNotFoundError("no dns-sd")

    adv = MacAdvertiser(spawn=spawn)
    assert adv.update(INFO) is False and adv.available is False


# --------------------------------------------------------------- LaunchAgent


def test_launch_agent_plist():
    data = agent_plist("/Applications/ShoutAbout.app/Contents/MacOS/ShoutAbout")
    assert data["Label"] == "fyi.shoutabout.mac"
    assert data["ProgramArguments"][1:] == ["run"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is False
    assert data["AssociatedBundleIdentifiers"] == ["fyi.shoutabout.mac"]


def test_open_at_login_on_and_off(tmp_path):
    exe = "/Applications/ShoutAbout.app/Contents/MacOS/ShoutAbout"
    mgr = MacStartupManager(folder=tmp_path, executable=exe)
    assert mgr.state().enabled is False and mgr.state().editable
    state = mgr.set_enabled(True)
    assert state.enabled
    with mgr.path.open("rb") as f:
        assert plistlib.load(f)["ProgramArguments"][0] == exe
    other = MacStartupManager(folder=tmp_path, executable="/other/ShoutAbout")
    assert other.state().enabled is False and "Another copy" in other.state().note
    assert mgr.set_enabled(False).enabled is False and not mgr.path.exists()
    mgr.set_enabled(False)  # already off: fine


def test_open_at_login_from_source_is_read_only(tmp_path):
    mgr = MacStartupManager(folder=tmp_path, executable=None)
    assert mgr.state().editable is False
    with pytest.raises(PermissionError):
        mgr.set_enabled(True)


# ---------------------------------------------------------------- decoding


def write_float_wav(path: Path, samples: np.ndarray, rate: int, channels: int) -> None:
    data = np.asarray(samples, dtype="<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * channels * 4, channels * 4, 32)
    body = (
        b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)


def fake_afconvert(rate=44_100, channels=2, seconds=1.0, fail=False):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == decode_mac.AFINFO:
            return subprocess.CompletedProcess(
                argv, 0, f"estimated duration: {seconds:.3f} sec\n", ""
            )
        if fail:
            return subprocess.CompletedProcess(argv, 1, "", "Error: unsupported format\n")
        n = int(rate * seconds)
        t = np.arange(n) / rate
        tone = 0.3 * np.sin(2 * np.pi * 440 * t)
        inter = np.repeat(tone, channels)
        write_float_wav(Path(argv[-1]), inter, rate, channels)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run, calls


def test_afconvert_command_and_duration_parsing():
    src, dst = Path("/tmp/in.m4a"), Path("/tmp/out.wav")
    cmd = decode_mac.convert_command(src, dst)
    assert cmd == ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEF32", str(src), str(dst)]
    assert decode_mac.parse_duration(
        "File: x\nestimated duration: 12.345678 sec\n"
    ) == pytest.approx(12.345678)
    assert decode_mac.parse_duration("nothing") is None


def test_decode_reads_afconverts_wav(tmp_path):
    run, calls = fake_afconvert()
    decoded = decode_mac.decode(tmp_path / "in.m4a", run=run)
    assert decoded.rate == 44_100 and decoded.channels == 2
    assert decoded.samples.size == 2 * 44_100
    assert calls[0][:5] == ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEF32"]


def test_decode_failure_is_an_error(tmp_path):
    run, _ = fake_afconvert(fail=True)
    with pytest.raises(decode_mac.CoreAudioError, match="unsupported"):
        decode_mac.decode(tmp_path / "in.m4a", run=run)


def m4a_head(path: Path) -> Path:
    path.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64)
    return path


def test_audio_chain_uses_core_audio_on_a_mac(tmp_path, monkeypatch):
    run, _ = fake_afconvert(rate=48_000, channels=2, seconds=1.0)
    monkeypatch.setattr(decode_mac, "probe_seconds", lambda p: 1.0)
    original = decode_mac.decode
    monkeypatch.setattr(decode_mac, "decode", lambda p: original(p, run=run))
    result = audio.decode_audio_info(
        m4a_head(tmp_path / "clip.m4a"), 600, backends=(audio.COREAUDIO,)
    )
    assert result.backend == audio.COREAUDIO
    assert result.samples.size == pytest.approx(16_000, abs=2)  # mono 16 kHz
    assert result.source_rate == 48_000 and result.channels == 2


def test_core_audio_refuses_too_long_before_decoding(tmp_path, monkeypatch):
    monkeypatch.setattr(decode_mac, "probe_seconds", lambda p: 900.0)
    monkeypatch.setattr(decode_mac, "decode", lambda p: pytest.fail("decoded anyway"))
    with pytest.raises(audio.AudioTooLongError):
        audio.decode_audio_info(m4a_head(tmp_path / "long.m4a"), 600, backends=(audio.COREAUDIO,))


def test_core_audio_isnt_handed_unknown_formats(tmp_path, monkeypatch):
    monkeypatch.setattr(decode_mac, "decode", lambda p: pytest.fail("decoded anyway"))
    junk = tmp_path / "x.bin"
    junk.write_bytes(b"\x01\x02\x03" * 40)
    with pytest.raises(audio.AudioError):
        audio.decode_audio_info(junk, 600, backends=(audio.COREAUDIO,))


def test_default_backends_on_a_mac(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    assert audio.default_backends() == (audio.WAV, audio.COREAUDIO, audio.PYAV)


# ------------------------------------------------------------ Apple Silicon


def sysctl_runner(values):
    def run(argv, **kwargs):
        name = argv[-1]
        if name in values:
            return subprocess.CompletedProcess(argv, 0, f"{values[name]}\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "unknown oid")

    return run


def test_probe_apple_chip():
    run = sysctl_runner(
        {
            "machdep.cpu.brand_string": "Apple M2 Pro",
            "hw.perflevel0.physicalcpu": 8,
            "hw.perflevel1.physicalcpu": 4,
        }
    )
    chip = sysinfo.probe_apple_chip(run=run, machine="arm64")
    assert chip == sysinfo.AppleChip("Apple M2 Pro", 8, 4)
    assert chip.generation == 2 and chip.variant == "Pro"
    assert sysinfo.probe_apple_chip(run=run, machine="x86_64") is None
    old = sysctl_runner({"hw.physicalcpu": 8})
    assert sysinfo.probe_apple_chip(run=old, machine="arm64").performance_cores == 4


def apple_hw(name="Apple M1", perf=4, cores=8, ram=8.0):
    chip = sysinfo.AppleChip(name, perf, cores - perf)
    probes = hardware.Probes(
        nvml=lambda: pytest.fail("no NVIDIA probing on a Mac"),
        smi=lambda: None,
        cuda=lambda: pytest.fail("no CUDA on a Mac"),
        cpu_name=lambda: "arm",
        cores=lambda: (cores, cores),
        ram_bytes=lambda: int(ram * hardware.GIB),
        apple=lambda: chip,
    )
    return hardware.detect(probes=probes)


def test_hardware_detects_apple_silicon():
    hw = apple_hw("Apple M3 Max", perf=12, cores=16, ram=48)
    assert hw.apple_chip == "Apple M3 Max" and hw.performance_cores == 12 and hw.gpus == ()
    assert "Apple M3 Max, 12 performance cores" in hw.summary()


def test_apple_silicon_tiers():
    base = modelpick.choose(apple_hw("Apple M1", perf=4, cores=8, ram=8))
    assert (base.tier, base.stt_model, base.device, base.compute_type) == (
        "balanced",
        "small.en",
        "cpu",
        "int8",
    )
    assert base.speed == "ok" and base.cpu_threads == 4 and not base.cleanup
    assert (
        base.explanation
        == "Your Apple M1 (4 performance cores) runs a fast, accurate speech model."
    )
    big = modelpick.choose(apple_hw("Apple M3 Max", perf=12, cores=16, ram=48))
    assert big.stt_model == "small.en" and big.cpu_threads == modelpick.APPLE_MAX_THREADS
    weak = modelpick.choose(apple_hw("Apple M1", perf=4, cores=8, ram=4))
    assert (weak.tier, weak.stt_model, weak.speed) == ("light", "base.en", "slow")


def test_apple_silicon_manual_tiers():
    m1 = apple_hw("Apple M1", perf=4, cores=8, ram=8)
    best = modelpick.choose_tier("best", m1)
    assert best.stt_model == "large-v3-turbo" and best.device == "cpu" and best.speed == "slow"
    assert "slow on this Mac" in best.explanation
    pro = apple_hw("Apple M2 Max", perf=8, cores=12, ram=32)
    assert modelpick.choose_tier("best", pro).speed == "ok"
    assert modelpick.choose_tier("light", pro).stt_model == "base.en"


def test_resolve_on_a_mac_with_an_auto_config():
    choice = modelpick.resolve(Config(), apple_hw(), probe_ollama=False)
    assert choice.device == "cpu" and choice.stt_model == "small.en" and choice.speed == "ok"
    assert choice.source == "auto"


# ----------------------------------------------------------------- config


def test_config_accepts_mac_hotkeys():
    cfg = parse_config('[dictation]\nhotkey = "right_cmd"\nmode = "toggle"\n')
    assert cfg.dictation.hotkey == "right_cmd"
    with pytest.raises(ConfigError):
        parse_config('[dictation]\nhotkey = "left_banana"\n')


def test_mac_paths(monkeypatch, tmp_path):
    from shoutabout import config

    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.delenv("SHOUTABOUT_HOME", raising=False)
    assert config.default_home().parts[-3:] == ("Library", "Application Support", "ShoutAbout")
    assert Paths(config.mac_home()).log_dir.parts[-3:] == ("Library", "Logs", "ShoutAbout")
    assert Paths(tmp_path).log_dir == tmp_path / "logs"  # a custom home keeps its own


# --------------------------------------------------------------- self-test


def test_platform_markers_filter_dependencies():
    from shoutabout.selftest import platform_applies, runtime_requirements

    spec = "pyobjc-core>=10.3; sys_platform == 'darwin'"
    assert platform_applies(spec, "darwin") and not platform_applies(spec, "win32")
    assert platform_applies("numpy>=1.26", "win32")
    assert not platform_applies("x; sys_platform != 'win32'", "win32")
    names = runtime_requirements([spec, "numpy>=1.26"])
    assert names == (["pyobjc-core", "numpy"] if sys.platform == "darwin" else ["numpy"])


# ----------------------------------------------------------------- Tk copy


@pytest.mark.parametrize(
    ("windows", "mac"),
    [
        ("Talk to this PC", "Talk to this Mac"),
        ("Sam's iPhone wants to dictate to this PC.", "Sam's iPhone wants to dictate to this Mac."),
        ("Start with Windows", "Open at login"),
        ("ShoutAbout lives in your system tray.", "ShoutAbout lives in your menu bar."),
        ("Starts when you sign in.", "Starts when you log in."),
        (
            "Everything runs on this PC. Your voice never leaves it.",
            "Everything runs on this Mac. Your voice never leaves it.",
        ),
        ("PCs and phones", "Macs and phones"),
        ("EPCOT", "EPCOT"),
    ],
)
def test_mac_copy(windows, mac):
    assert tkmac.mac_copy(windows) == mac


def test_rewrite_options_only_touches_text_and_title():
    out = tkmac._rewrite_options(("-text", "This PC", "-bg", "PC", "-title", "PC"))
    assert out == ("-text", "This Mac", "-bg", "PC", "-title", "Mac")


# ------------------------------------------------------- menu and permissions


def test_permission_report_and_menu_lines():
    report = permissions.PermissionReport("denied", False, False)
    assert report.missing() == ["microphone", "accessibility", "input_monitoring"]
    assert [label for _n, label in platform.permission_items(report)] == [
        "Allow the microphone…",
        "Allow pasting (Accessibility)…",
        "Allow the shortcut key (Input Monitoring)…",
    ]
    ok = permissions.PermissionReport("granted", True, False)
    assert ok.missing() == [] and ok.can_paste and ok.can_hear_hotkey
    assert permissions.SETTINGS_URLS["accessibility"].endswith("Privacy_Accessibility")
    with pytest.raises(ValueError):
        permissions.open_settings("camera")


def test_menu_entries_put_permission_lines_after_the_status():
    state = HubState()
    actions = TrayActions(toggle_pause=lambda: None, quit=lambda: None, settings=lambda: None)
    rows = menu_entries(state, actions, "Press Right ⌘ Command to talk.", [(0, "Allow pasting…")])
    labels = [label for _id, label, _en, sep in rows if not sep]
    assert labels[1] == "Press Right ⌘ Command to talk."
    assert labels[2] == "Allow pasting…"
    assert rows[2][0] == EXTRA_BASE and rows[2][2] is True
    assert labels[-1] == "Quit" and "Settings…" in labels


def test_applescript_string_escapes():
    assert applescript_string('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


def test_dictation_factories_are_empty_off_macos():
    if sys.platform != "darwin":
        assert platform.dictation_factories() == {}


def test_first_run_defaults_set_the_mac_hotkey(tmp_path):
    from shoutabout.config import ensure_initialized, load_config

    paths = Paths(tmp_path)
    ensure_initialized(paths)
    platform.first_run_defaults(paths, fresh=False)
    assert load_config(paths).dictation.hotkey == "right_ctrl"  # existing installs: untouched
    platform.first_run_defaults(paths, fresh=True)
    cfg = load_config(paths)
    assert (cfg.dictation.hotkey, cfg.dictation.mode) == (keys.DEFAULT_MAC_HOTKEY, "toggle")


def test_learn_defaults_never_scan_guarded_folders(tmp_path):
    assert platform.learn_defaults(tmp_path) == {"seed_projects": False}
    (tmp_path / "Documents").mkdir()
    assert platform.learn_defaults(tmp_path) == {"seed_projects": False}
    (tmp_path / "Developer").mkdir()
    (tmp_path / "source" / "repos").mkdir(parents=True)
    roots = platform.learn_defaults(tmp_path)["project_roots"]
    assert roots == ["~/Developer", "~/source/repos"]
    assert all("Documents" not in r and "Desktop" not in r for r in platform.MAC_PROJECT_ROOTS)


def test_doctor_rows_say_where_the_answer_comes_from():
    rows = doctor_mac.permission_rows(permissions.PermissionReport("granted", True, None))
    assert [r[1] for r in rows] == ["PASS", "PASS", "PASS"]
    assert all("as macOS sees" in r[2] for r in rows[:2])
    bad = doctor_mac.permission_rows(permissions.PermissionReport("denied", False, False), True)
    assert [r[1] for r in bad] == ["FAIL", "FAIL", "WARN"] and bad[1][3]


# --------------------------------------------------------------- packaging


def load_packaging(name: str):
    import importlib.util

    path = REPO / "packaging" / "macos" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"mac_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_icns_file_structure(tmp_path):
    icns = load_packaging("make_icns")
    out = icns.build(tmp_path / "icon.icns", slots=((b"icp4", 16), (b"ic11", 32)))
    data = out.read_bytes()
    assert data[:4] == b"icns" and struct.unpack(">I", data[4:8])[0] == len(data)
    assert data[8:12] == b"icp4"
    first = struct.unpack(">I", data[12:16])[0]
    assert data[16:24] == b"\x89PNG\r\n\x1a\n"
    assert data[8 + first : 8 + first + 4] == b"ic11"
    img = icns.icon_image(64)
    rgba = np.frombuffer(img.rgba, dtype=np.uint8).reshape(64, 64, 4)
    assert rgba[0, 0, 3] == 0 and rgba[32, 32, 3] == 255  # transparent margin, opaque tile


def test_mac_bundle_rules(tmp_path):
    rules = load_packaging("mac_rules")
    app = tmp_path / "ShoutAbout.app" / "Contents"
    (app / "Frameworks" / "_sounddevice_data" / "portaudio-binaries").mkdir(parents=True)
    keep = app / "Frameworks" / "_sounddevice_data" / "portaudio-binaries" / "libportaudio.dylib"
    keep.write_bytes(b"x")
    assert rules.bad_files(tmp_path) == []
    (app / "Frameworks" / "libavcodec.61.dylib").write_bytes(b"x")
    (app / "Frameworks" / "thing.dll").write_bytes(b"x")
    assert [p.name for p in rules.bad_files(tmp_path)] == ["libavcodec.61.dylib", "thing.dll"]
    assert rules.main(["check", str(tmp_path)]) == 1


def test_build_script_basics():
    script = (REPO / "packaging" / "macos" / "build.sh").read_text("utf-8")
    assert script.startswith("#!/bin/bash")
    assert "\r" not in script
    assert "set -euo pipefail" in script
    assert "codesign --force --deep --sign -" in script
    assert "--locked" in script and "--extra cuda" not in script
    assert " git " not in script.replace("(No git", "")  # git may pop up the developer tools


def test_identity_reload(tmp_path):
    from shoutabout.identity import IdentityStore

    a = IdentityStore(tmp_path / "identity.json", default_name=lambda: "Mac")
    b = IdentityStore(tmp_path / "identity.json", default_name=lambda: "Mac")
    assert a.get().id == b.get().id
    b.rename("Office Mac")
    assert a.get().name == "Mac"  # cached
    a.reload()
    assert a.get().name == "Office Mac"


def test_logged_pipeline_logs_timings_without_text(caplog, monkeypatch):
    from shoutabout.models import CleanupResult, Timings, TranscribeResult

    monkeypatch.setattr(platform.sys, "platform", "darwin")
    result = TranscribeResult(
        text="secret words",
        raw_text="secret words",
        target="pc",
        route="local",
        pasted=True,
        warning=None,
        cleanup=CleanupResult(text="secret words", status="disabled"),
        timings=Timings(stt=1234),
    )
    inner = SimpleNamespace(process_audio=lambda *a, **k: result, cleaner=None)
    get = platform.logged_pipeline(lambda: inner)
    caplog.set_level("INFO", logger="shoutabout.mac.platform")
    assert get().process_audio(None, target="pc", route="local") is result
    assert get().cleaner is None
    assert "stt=1234" in caplog.text and "secret" not in caplog.text
    assert platform.logged_pipeline(lambda: None)() is None
