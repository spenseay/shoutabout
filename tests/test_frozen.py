"""The installed-app entry point (shoutabout.frozen) and frozen-bundle path handling."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from shoutabout import frozen
from shoutabout.engine import cuda_dlls


@pytest.fixture
def fake_bundle(tmp_path, monkeypatch):
    """Pretend to be ShoutAbout.exe in <tmp>/app with its bundle in <tmp>/app/_internal."""
    app = tmp_path / "app"
    bundle = app / "_internal"
    bundle.mkdir(parents=True)
    exe = app / "ShoutAbout.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return bundle


def test_not_frozen_from_source():
    assert frozen.is_frozen() is False
    assert frozen.bundle_dir() is None
    assert frozen.app_dir() is None


def test_frozen_paths(fake_bundle):
    assert frozen.is_frozen() is True
    assert frozen.bundle_dir() == fake_bundle
    assert frozen.app_dir() == fake_bundle.parent.resolve()


def test_app_icon_path(fake_bundle):
    assert frozen.app_icon_path() is None
    (fake_bundle / frozen.ICON_NAME).write_bytes(b"\0\0\1\0")
    assert frozen.app_icon_path() == fake_bundle / frozen.ICON_NAME


def test_app_command_frozen_is_the_exe(fake_bundle):
    assert frozen.app_command() == [str((fake_bundle.parent / "ShoutAbout.exe").resolve())]


def test_app_command_from_source_runs_the_hub():
    cmd = frozen.app_command()
    assert cmd[1:] == ["-m", "shoutabout", "run"]
    if sys.platform == "win32" and Path(sys.executable).with_name("pythonw.exe").is_file():
        assert Path(cmd[0]).name.lower() == "pythonw.exe"  # no console window at sign-in


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], None),
        (["run"], "run"),
        (["--home", "C:\\x", "run", "--no-tray"], "run"),
        (["--home", "run", "doctor"], "doctor"),  # "run" is the --home value here
        (["--version"], None),
        (["doctor", "--skip-model"], "doctor"),
    ],
)
def test_command_name(args, expected):
    assert frozen.command_name(args) == expected


@pytest.fixture
def calls(monkeypatch):
    seen: dict[str, list] = {"cli": [], "selftest": [], "attach": [], "aumid": []}
    import shoutabout.cli
    import shoutabout.selftest

    monkeypatch.setattr(shoutabout.cli, "main", lambda args: seen["cli"].append(list(args)) or 7)
    monkeypatch.setattr(
        shoutabout.selftest, "main", lambda args: seen["selftest"].append(list(args)) or 3
    )
    monkeypatch.setattr(frozen, "attach_parent_console", lambda: seen["attach"].append(1))
    monkeypatch.setattr(frozen, "set_app_user_model_id", lambda: seen["aumid"].append(1))
    return seen


def test_main_without_arguments_runs_the_hub(calls):
    assert frozen.main([]) == 7
    assert calls["cli"] == [["run", "--greet"]]  # the user opened it: say it's ready
    assert calls["attach"] == []  # launched from Explorer / sign-in: no console to attach
    assert calls["aumid"] == [1]


@pytest.mark.parametrize("args", [["run"], ["--home", "C:\\x", "run"], ["run", "--home", "C:\\x"]])
def test_main_run_is_exactly_a_double_click(calls, monkeypatch, args):
    # The sign-in Run value (installer and settings window) is '"<exe>" run'.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert frozen.main(args) == 7
    assert calls["cli"] == [args]
    assert calls["attach"] == []  # never takes over a console
    assert calls["aumid"] == [1]
    assert sys.stdout is None  # left for run_hub, which logs stray output to stdout.log
    frozen.main([])
    assert calls["attach"] == [] and calls["aumid"] == [1, 1]


def test_main_run_without_tray_is_a_console_session(calls):
    assert frozen.main(["run", "--no-tray"]) == 7
    assert calls["attach"] == [1]
    assert calls["aumid"] == []


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], True),
        (["run"], True),
        (["--home", "x", "run"], True),
        (["run", "--no-tray"], False),
        (["doctor"], False),
        (["--version"], False),
        (["--home", "run"], False),  # "run" is the --home value
    ],
)
def test_is_hub_launch(args, expected):
    assert frozen.is_hub_launch(args) is expected


def test_installer_run_value_matches_the_hub_launch():
    iss = (Path(__file__).resolve().parents[1] / "packaging" / "installer.iss").read_text("ascii")
    assert "'\\\" run\" /f'" in iss  # reg.exe ... /d "\"<exe>\" run"
    assert frozen.is_hub_launch(["run"])


def test_main_self_test(calls):
    assert frozen.main(["--self-test", "--home", "x", "--out", "y.json"]) == 3
    assert calls["selftest"] == [["--home", "x", "--out", "y.json"]]
    assert calls["cli"] == []


def test_main_passes_other_commands_to_the_cli(calls):
    assert frozen.main(["doctor", "--skip-model"]) == 7
    assert calls["cli"] == [["doctor", "--skip-model"]]
    assert calls["attach"] == [1]


def test_main_cli_never_crashes_without_streams(calls, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    frozen.main(["info"])
    assert sys.stdout is not None and sys.stderr is not None
    print("this must not raise")


def test_main_run_leaves_missing_streams_for_the_hub_log(calls, monkeypatch):
    # run_hub sends stray output to <home>/logs/stdout.log when the streams are None.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    frozen.main([])
    assert sys.stdout is None


def test_ensure_std_streams_keeps_existing(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", None)
    frozen.ensure_std_streams()
    assert sys.stdout is out
    assert sys.stderr is not None


def test_attach_parent_console_is_a_no_op_with_streams():
    # pytest's captured streams exist, so nothing is attached (and nothing replaced).
    before = sys.stdout
    assert frozen.attach_parent_console() is False
    assert sys.stdout is before


def test_cuda_dlls_find_the_bundled_nvidia_folder(fake_bundle):
    bin_dir = fake_bundle / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "cublas64_12.dll").write_bytes(b"MZ")
    (fake_bundle / "nvidia" / "empty" / "bin").mkdir(parents=True)  # no DLLs: ignored
    dirs = cuda_dlls.find_nvidia_dll_dirs()
    assert str(bin_dir.resolve()).lower() in {str(Path(d).resolve()).lower() for d in dirs}
    assert all("empty" not in d for d in dirs)


def test_cuda_dlls_frozen_roots_only_when_frozen(fake_bundle, monkeypatch):
    assert cuda_dlls._frozen_roots() == [fake_bundle / "nvidia"]
    monkeypatch.setattr(sys, "frozen", False)
    assert cuda_dlls._frozen_roots() == []


def test_entry_script_calls_frozen_main():
    entry = Path(__file__).resolve().parents[1] / "packaging" / "entry.py"
    text = entry.read_text("utf-8")
    assert "from shoutabout.frozen import main" in text
    assert "freeze_support()" in text
    compile(text, str(entry), "exec")
