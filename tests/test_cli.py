from __future__ import annotations

import json
import sys

import httpx
import pytest

from fakes import FakeEngine, FakeVocabStore, fake_decode
from shoutabout import cli, doctor
from shoutabout.config import Paths, load_token
from shoutabout.doctor import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    Check,
    check_health,
    check_public_url,
    format_check,
    interpret_block_rules,
    interpret_cloudflared,
    interpret_network_profiles,
    interpret_port_rule,
    interpret_program_rules,
    normalize_public_url,
)

INFO_KEYS = {
    "home",
    "config_file",
    "vocab_file",
    "token_file",
    "log_dir",
    "port",
    "host",
    "lan_ip",
    "lan_url",
    "stt_model",
    "stt_device",
    "stt_tier",
    "cleanup_enabled",
    "cleanup_model",
    "ollama_url",
    "access_enabled",
    "public_hostname",
}


@pytest.fixture
def fixed_lan(monkeypatch):
    from shoutabout import netinfo

    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")


# ----------------------------------------------------------------------- commands


def test_info_json(home, capsys, fixed_lan):
    assert cli.main(["info", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert set(info) == INFO_KEYS
    assert info["home"] == str(home)
    assert info["port"] == 47821
    assert info["lan_ip"] == "192.168.1.50"
    assert info["lan_url"] == "http://192.168.1.50:47821"
    assert info["access_enabled"] is False
    assert info["public_hostname"] is None


def test_info_never_shows_token(home, capsys, fixed_lan):
    cli.main(["init"])
    token = load_token(Paths(home))
    capsys.readouterr()
    cli.main(["info"])
    cli.main(["info", "--json"])
    out = capsys.readouterr().out
    assert token not in out
    assert "lan_url" in out


def test_info_reads_install_state(home, capsys, fixed_lan):
    home.mkdir(parents=True)
    (home / "install-state.json").write_text(
        '\ufeff{"public_hostname": "dictate.example.com", "port": 8765}', "utf-8"
    )
    cli.main(["info", "--json"])
    assert json.loads(capsys.readouterr().out)["public_hostname"] == "dictate.example.com"


def test_home_option_overrides_env(tmp_path, capsys, home, fixed_lan):
    other = tmp_path / "other"
    assert cli.main(["--home", str(other), "info", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["home"] == str(other)
    assert cli.main(["info", "--json", "--home", str(other)]) == 0
    assert json.loads(capsys.readouterr().out)["home"] == str(other)


def test_init_creates_files(home, capsys):
    assert cli.main(["init"]) == 0
    paths = Paths(home)
    for p in (paths.config_file, paths.vocab_file, paths.token_file, paths.log_dir):
        assert p.exists()
    out = capsys.readouterr().out
    assert "created" in out
    assert load_token(paths) not in out
    assert cli.main(["init"]) == 0
    assert "exists" in capsys.readouterr().out


def test_init_rotate_token(home, capsys):
    cli.main(["init"])
    old = load_token(Paths(home))
    assert cli.main(["init", "--rotate-token"]) == 0
    assert load_token(Paths(home)) != old
    assert "rotated" in capsys.readouterr().out


def test_token_prints_token(home, capsys):
    assert cli.main(["token"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out) >= 32
    assert out == load_token(Paths(home))


def test_token_qr(home, capsys):
    assert cli.main(["token", "--qr"]) == 0
    out = capsys.readouterr().out
    assert load_token(Paths(home)) not in out
    assert len(out.splitlines()) > 10


def test_qr_text(capsys):
    assert cli.main(["qr", "hello"]) == 0
    assert "hello" not in capsys.readouterr().out


def test_qr_requires_input(capsys):
    assert cli.main(["qr"]) == 1
    assert "error" in capsys.readouterr().err


def test_usage_errors_exit_2(capsys):
    assert cli.main([]) == 2
    assert cli.main(["nonsense"]) == 2
    assert cli.main(["token", "--copy", "--qr"]) == 2


def test_help_exit_0(capsys):
    assert cli.main(["--help"]) == 0
    assert "transcribe" in capsys.readouterr().out


def test_bad_config_is_error(home, capsys):
    home.mkdir(parents=True)
    (home / "config.toml").write_text("[server]\nport = 'x'\n", "utf-8")
    assert cli.main(["info"]) == 1
    assert "error:" in capsys.readouterr().err


# ------------------------------------------------------------ transcribe helpers


def test_word_accuracy():
    assert cli.word_accuracy("Hello, Better Shell!", "hello better shell") == 1.0
    assert cli.word_accuracy("it's fine", "its fine") == 1.0
    assert cli.word_accuracy("one two three four", "one two three five") == pytest.approx(0.75)
    assert cli.word_accuracy("", "") == 1.0
    assert cli.word_accuracy("a b", "") == 0.0


def test_term_hits_case_sensitive():
    hits = cli.term_hits("I use Better Shell and claude code", ["Better Shell", "Claude Code"])
    assert hits == {"Better Shell": True, "Claude Code": False}


def test_expand_inputs(tmp_path):
    d = tmp_path / "audio"
    d.mkdir()
    for name in ("b.m4a", "A.wav", "c.txt", "d.MP3", "manifest.json"):
        (d / name).write_bytes(b"x")
    files, problems = cli.expand_inputs([str(d)])
    assert [f.name for f in files] == ["A.wav", "b.m4a", "d.MP3"]
    assert problems == []

    files, problems = cli.expand_inputs([str(d / "*.m4a"), str(d / "b.m4a"), str(d / "nope.wav")])
    assert [f.name for f in files] == ["b.m4a"]  # de-duplicated
    assert problems == [f"not found: {d / 'nope.wav'}"]

    files, problems = cli.expand_inputs([str(d / "*.flac"), str(tmp_path)])
    assert files == []
    assert len(problems) == 2


def test_manifest_and_evaluation(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "vocab-01.m4a",
                    "text": "Open Better Shell now.",
                    "kind": "vocab",
                    "expect_terms": ["Better Shell"],
                },
                {"bogus": True},
            ]
        ),
        "utf-8",
    )
    entries = cli.load_manifest(tmp_path)
    assert set(entries) == {"vocab-01.m4a"}
    ev = cli.evaluate_against_manifest(
        entries["vocab-01.m4a"], "open better shell now", "Open Better Shell now."
    )
    assert ev["word_accuracy"] == 1.0
    assert ev["terms"] == {"Better Shell": True}
    assert ev["terms_raw"] == {"Better Shell": False}
    assert cli.load_manifest(tmp_path / "missing") == {}


def test_transcribe_end_to_end_with_fakes(tmp_path, home, capsys, monkeypatch):
    import shoutabout.audio
    import shoutabout.engine
    import shoutabout.vocab

    monkeypatch.setattr(
        shoutabout.engine, "create_engine", lambda cfg: FakeEngine("open better shell now")
    )
    monkeypatch.setattr(
        shoutabout.vocab, "VocabStore", lambda path: FakeVocabStore(), raising=False
    )
    monkeypatch.setattr(shoutabout.audio, "decode_audio", fake_decode)
    d = tmp_path / "test-audio"
    d.mkdir()
    (d / "vocab-01.m4a").write_bytes(b"x")
    (d / "other.wav").write_bytes(b"x")
    (d / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "vocab-01.m4a",
                    "text": "Open Better Shell now.",
                    "kind": "vocab",
                    "expect_terms": ["Better Shell"],
                }
            ]
        ),
        "utf-8",
    )
    assert cli.main(["transcribe", "--no-cleanup", "--json", str(d)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 2
    assert lines[0]["file"].endswith("other.wav")
    assert lines[1]["file"].endswith("vocab-01.m4a")
    by_name = {line["file"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: line for line in lines}
    assert by_name["vocab-01.m4a"]["word_accuracy"] == 1.0
    assert by_name["vocab-01.m4a"]["terms"] == {"Better Shell": False}
    assert by_name["vocab-01.m4a"]["cleanup"] == "disabled"
    assert "expected" not in by_name["other.wav"]

    assert cli.main(["transcribe", "--no-cleanup", str(d / "vocab-01.m4a")]) == 0
    out = capsys.readouterr().out
    assert "raw:" in out and "expected: Open Better Shell now." in out
    assert "Better Shell [MISS]" in out


def test_transcribe_missing_file(home, capsys):
    assert cli.main(["transcribe", "does-not-exist.m4a"]) == 1
    assert "not found" in capsys.readouterr().err


# ------------------------------------------------------------------------- doctor


def test_format_check():
    assert format_check(Check("Config", PASS, r"C:\x\config.toml", fix="ignored")) == (
        "[PASS] Config — C:\\x\\config.toml"
    )
    assert format_check(Check("Ollama", FAIL, "not reachable", fix="start it\nor this")) == (
        "[FAIL] Ollama — not reachable\n       fix: start it\n            or this"
    )
    assert format_check(Check("X", SKIP)) == "[SKIP] X"


def test_interpret_port_rule():
    rule = {
        "Name": "R",
        "Enabled": "True",
        "Direction": "Inbound",
        "Action": "Allow",
        "Profile": "Private",
        "LocalPort": "8765",
    }
    assert interpret_port_rule(rule, "R", 8765).status == PASS
    assert interpret_port_rule({**rule, "Profile": "Domain, Private"}, "R", 8765).status == PASS
    assert interpret_port_rule(None, "R", 8765).status == FAIL
    assert "New-NetFirewallRule" in interpret_port_rule(None, "R", 8765).fix
    for bad in (
        {"Enabled": "False"},
        {"Profile": "Public"},
        {"Action": "Block"},
        {"LocalPort": "9999"},
    ):
        assert interpret_port_rule({**rule, **bad}, "R", 8765).status == FAIL


def test_interpret_program_rules():
    names = ["R-python", "R-pythonw"]
    rules = [{"Name": n, "Enabled": "True"} for n in names]
    assert interpret_program_rules(rules, names).status == PASS
    check = interpret_program_rules(rules[:1], names)
    assert check.status == WARN
    assert "allow Python" in check.detail


def test_interpret_block_rules():
    allow = {
        "Name": "a",
        "Enabled": "True",
        "Direction": "Inbound",
        "Action": "Allow",
        "Profile": "Public",
        "Program": "python.exe",
    }
    assert interpret_block_rules([allow]).status == PASS
    block = {**allow, "Name": "TCP Query User{X}", "Action": "Block", "Profile": "Private"}
    check = interpret_block_rules([allow, block])
    assert check.status == FAIL
    assert "Remove-NetFirewallRule -Name 'TCP Query User{X}'" in check.fix
    assert interpret_block_rules([{**block, "Profile": "Public"}]).status == WARN
    assert interpret_block_rules([{**block, "Enabled": "False"}]).status == PASS


def test_interpret_network_profiles():
    wifi = {
        "InterfaceAlias": "Wi-Fi",
        "Name": "home",
        "NetworkCategory": "Private",
        "IPv4Connectivity": "Internet",
        "IPv4": ["192.168.1.50"],
    }
    wsl = {
        "InterfaceAlias": "vEthernet (WSL)",
        "Name": "Unidentified",
        "NetworkCategory": "Public",
        "IPv4Connectivity": "NoTraffic",
        "IPv4": ["172.31.0.1"],
    }
    assert interpret_network_profiles([wifi, wsl], "192.168.1.50").status == PASS
    public = {**wifi, "NetworkCategory": "Public"}
    check = interpret_network_profiles([public], "192.168.1.50")
    assert check.status == FAIL
    assert 'Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private' in check.fix
    assert interpret_network_profiles([public], None).status == WARN
    assert interpret_network_profiles([], None).status == WARN


def test_interpret_cloudflared():
    assert (
        interpret_cloudflared({"Status": "Running", "StartType": "Automatic"}, True).status == PASS
    )
    assert (
        interpret_cloudflared({"Status": "Stopped", "StartType": "Automatic"}, True).status == FAIL
    )
    assert interpret_cloudflared({"Status": "Running", "StartType": "Manual"}, True).status == WARN
    assert interpret_cloudflared(None, True).status == FAIL
    assert interpret_cloudflared(None, False).status == SKIP


def test_normalize_public_url():
    assert normalize_public_url("dictate.example.com") == "https://dictate.example.com/health"
    assert normalize_public_url("https://d.example.com/") == "https://d.example.com/health"
    assert normalize_public_url("https://d.example.com/health") == "https://d.example.com/health"


def mock(handler):
    return httpx.MockTransport(handler)


def test_public_url_end_to_end_pass():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    check = check_public_url(
        "https://d.example.com/health",
        client_id="cid",
        client_secret="s3cret",
        transport=mock(handler),
    )
    assert check.status == PASS
    assert "end-to-end" in check.detail
    assert seen["cf-access-client-id"] == "cid"
    assert "s3cret" not in format_check(check)


def test_public_url_reachable_without_token_is_a_failure():
    # The hub answering an unauthenticated public request means Access isn't in front of it.
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    check = check_public_url("https://d.example.com/health", transport=mock(handler))
    assert check.status == FAIL
    assert "not protecting" in check.detail


def test_doctor_warns_when_access_jwt_not_configured(tmp_path):
    from shoutabout.config import Paths, ensure_initialized
    from shoutabout.doctor import WARN, Doctor

    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    doctor = Doctor(paths, skip_model=True, skip_tunnel=False, skip_ollama=True, out=lambda s: 0)
    doctor.cfg = doctor.cfg if hasattr(doctor, "cfg") else None
    check = doctor.check_access_config()
    assert check.status == WARN
    assert "team_domain" in check.fix


def test_public_url_access_redirect_is_expected():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "https://team.cloudflareaccess.com/cdn-cgi/access/login/x"}
        )

    check = check_public_url("https://d.example.com/health", transport=mock(handler))
    assert check.status == PASS
    assert "blocking unauthenticated requests (expected)" in check.detail
    with_token = check_public_url(
        "https://d.example.com/health", client_id="a", client_secret="b", transport=mock(handler)
    )
    assert with_token.status == FAIL


def test_public_url_cloudflare_403_is_expected():
    def handler(request):
        return httpx.Response(
            403, text="<html>Forbidden</html>", headers={"server": "cloudflare", "cf-ray": "abc"}
        )

    assert check_public_url("https://d.example.com/health", transport=mock(handler)).status == PASS


@pytest.mark.parametrize("status", [502, 503, 530])
def test_public_url_tunnel_down(status):
    check = check_public_url(
        "https://d.example.com/health", transport=mock(lambda r: httpx.Response(status, text="err"))
    )
    assert check.status == FAIL
    assert "tunnel not reaching the hub" in check.detail


def test_public_url_connect_error():
    def handler(request):
        raise httpx.ConnectError("getaddrinfo failed", request=request)

    check = check_public_url("https://d.example.com/health", transport=mock(handler))
    assert check.status == FAIL
    assert "DNS" in check.fix


def test_public_url_hub_forbidden():
    check = check_public_url(
        "https://d.example.com/health",
        transport=mock(lambda r: httpx.Response(403, json={"detail": "forbidden"})),
    )
    assert check.status == FAIL


def test_check_health():
    ok = check_health(
        "Hub",
        "http://127.0.0.1:1/health",
        fix="start",
        transport=mock(lambda r: httpx.Response(200, json={"ok": True})),
    )
    assert ok.status == PASS

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    down = check_health("Hub", "http://127.0.0.1:1/health", fix="start it", transport=mock(refused))
    assert down.status == FAIL and down.fix == "start it"

    other = check_health(
        "Hub",
        "http://127.0.0.1:8765/health",
        fix="start it",
        transport=mock(lambda r: httpx.Response(404, json={"detail": "Not Found"})),
    )
    assert other.status == FAIL
    assert "something other than the ShoutAbout hub" in other.detail
    assert "Get-NetTCPConnection -LocalPort 8765" in other.fix

    forbidden = check_health(
        "Hub",
        "http://192.168.0.5:8765/health",
        fix="x",
        transport=mock(lambda r: httpx.Response(403, json={"detail": "forbidden"})),
    )
    assert "lan_networks" in forbidden.fix


def test_doctor_runs_all_checks_and_isolates_crashes(home, monkeypatch):
    cli.main(["init"])
    lines = []
    d = doctor.Doctor(
        Paths(home),
        skip_model=True,
        skip_tunnel=True,
        skip_ollama=True,
        out=lines.append,
        powershell=lambda script: [],
    )
    monkeypatch.setattr(d, "check_cuda", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(d, "check_hub_loopback", lambda: Check("Hub (loopback)", PASS, "ok"))
    monkeypatch.setattr(d, "check_hub_lan", lambda: Check("Hub (LAN URL)", PASS, "ok"))
    monkeypatch.setattr(d, "check_vocab", lambda: Check("Vocab", PASS, "7 terms"))
    rc = d.run()
    text = "\n".join(lines)
    assert rc == 1
    assert "[FAIL] CUDA — check crashed: RuntimeError: boom" in text
    assert text.splitlines()[0].startswith("[PASS] Config — ")
    assert "[PASS] Hub token — present" in text
    assert "[SKIP] Whisper model — skipped (--skip-model)" in text
    assert "[SKIP] Public URL" in text
    assert load_token(Paths(home)) not in text
    names = [c.name for c in d.results]
    assert names.index("Config") < names.index("Hub token") < names.index("Vocab")
    assert names.index("CUDA") < names.index("Whisper model") < names.index("Ollama")
    assert names.index("Hub (loopback)") < names.index("Hub (LAN URL)") < names.index("Public URL")
    if sys.platform == "win32":
        assert "[FAIL] Firewall rule" in text  # fake PowerShell returned no rules


def test_encode_m4a_command(tmp_path, monkeypatch, capsys):
    import wave

    from shoutabout import pyavdecode

    src = tmp_path / "in.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x10" * 16_000)
    assert cli.main(["encode-m4a", str(src), str(tmp_path / "out.m4a")]) == 0
    assert (tmp_path / "out.m4a").stat().st_size > 0
    # The installed app has no PyAV: a plain error, not a traceback.
    monkeypatch.setattr(pyavdecode, "available", lambda: False)
    capsys.readouterr()
    assert cli.main(["encode-m4a", str(src), str(tmp_path / "x.m4a")]) == 1
    assert "needs PyAV" in capsys.readouterr().err


def test_doctor_audio_decoding(home, monkeypatch):
    from shoutabout import audio

    d = doctor.Doctor(Paths(home), out=lambda s: None)
    check = d.check_audio()
    assert check.status == PASS
    expected = "Windows Media Foundation" if sys.platform == "win32" else "PyAV"
    assert expected in check.detail

    def unavailable(path, max_seconds=None, *, backends=None):
        raise audio.DecoderUnavailableError("Install the Media Feature Pack, then restart.")

    monkeypatch.setattr(audio, "decode_audio_info", unavailable)
    check = d.check_audio()
    assert check.status == FAIL and "Media Foundation is missing" in check.detail
    assert check.fix == "Install the Media Feature Pack, then restart."

    def broken(path, max_seconds=None, *, backends=None):
        raise audio.AudioError("damaged")

    monkeypatch.setattr(audio, "decode_audio_info", broken)
    assert d.check_audio().status == FAIL


def test_doctor_cuda_skipped_on_cpu(home):
    cli.main(["init"])
    paths = Paths(home)
    text = paths.config_file.read_text("utf-8").replace('device = "auto"', 'device = "cpu"')
    paths.config_file.write_text(text, "utf-8")
    d = doctor.Doctor(paths, out=lambda s: None)
    d.check_config()
    assert d.check_cuda().status == SKIP


# ------------------------------------------------------------ logging / hub bits


def test_setup_logging_idempotent(tmp_path):
    import logging

    from shoutabout.logging_setup import setup_logging

    paths = Paths(tmp_path)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        setup_logging(paths, "DEBUG", console=False)
        setup_logging(paths, "INFO", console=True)
        ours = [h for h in root.handlers if getattr(h, "_shoutabout_handler", False)]
        assert len(ours) == 2  # file + console, not duplicated
        logging.getLogger("shoutabout.test").warning("hello log")
        for h in ours:
            h.flush()
        text = (tmp_path / "logs" / "hub.log").read_text("utf-8")
        assert "WARNING shoutabout.test: hello log" in text
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_redirect_std_streams(tmp_path, monkeypatch):
    from shoutabout.logging_setup import redirect_std_streams

    paths = Paths(tmp_path)
    assert redirect_std_streams(paths) is None  # streams exist under pytest
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    stream = redirect_std_streams(paths)
    try:
        assert sys.stdout is stream and sys.stderr is stream
        print("progress bar output")
    finally:
        stream.close()
    assert "progress bar output" in (tmp_path / "logs" / "stdout.log").read_text("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows-only")
def test_single_instance_mutex():
    import uuid

    from shoutabout.hub import SingleInstance

    name = f"Local\\ShoutAboutTest-{uuid.uuid4().hex}"
    first, second = SingleInstance(name), SingleInstance(name)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    third = SingleInstance(name)
    assert third.acquire() is True
    third.release()


# -------------------------------------------------------------------------- vocab


def _vocab_home(tmp_path):
    home_dir = tmp_path / "vhome"
    home_dir.mkdir()
    (home_dir / "vocab.txt").write_text("# my vocab\nBetter Shell\nClaude Code\n", "utf-8")
    return home_dir


def test_vocab_add_list_remove(tmp_path, capsys):
    h = str(_vocab_home(tmp_path))
    assert cli.main(["vocab", "add", "--home", h, "Chameleon", "CV."]) == 0
    assert 'added "Chameleon CV"' in capsys.readouterr().out
    assert cli.main(["vocab", "add", "--home", h, "better", "shell"]) == 0
    assert "already in" in capsys.readouterr().out
    assert cli.main(["vocab", "list", "--home", h, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == {"terms": ["Chameleon CV", "Better Shell", "Claude Code"], "count": 3}
    assert cli.main(["vocab", "list", "--home", h]) == 0
    out = capsys.readouterr().out
    assert "  1. Chameleon CV" in out and "3 term(s)" in out
    assert cli.main(["vocab", "remove", "--home", h, "chameleon", "cv"]) == 0
    capsys.readouterr()
    assert cli.main(["vocab", "remove", "--home", h, "Nope"]) == 1
    assert "not in" in capsys.readouterr().err
    text = (tmp_path / "vhome" / "vocab.txt").read_text("utf-8")
    assert text == "# my vocab\nBetter Shell\nClaude Code\n"


def test_vocab_add_invalid_is_error(tmp_path, capsys):
    h = str(_vocab_home(tmp_path))
    assert cli.main(["vocab", "add", "--home", h, "#hashtag"]) == 1
    assert "comment" in capsys.readouterr().err
    assert cli.main(["vocab", "--home", h]) == 2  # no action given


def test_vocab_suggest_seed_dismiss(tmp_path, capsys):
    home_dir = _vocab_home(tmp_path)
    h = str(home_dir)
    projects = tmp_path / "projects"
    (projects / "chameleon-cv").mkdir(parents=True)
    (projects / "chameleon-cv" / "package.json").write_text('{"name": "chameleon-cv"}', "utf-8")
    (projects / "better_shell").mkdir(parents=True)
    (projects / "better_shell" / "package.json").write_text('{"name": "kestrel"}', "utf-8")
    (home_dir / "config.toml").write_text(
        f"[learn]\nproject_roots = [{json.dumps(str(projects))}]\n", "utf-8"
    )

    # Nothing is looked at unless asked: suggest only lists what's waiting.
    assert cli.main(["vocab", "suggest", "--home", h, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"new": 0, "pending": []}
    assert cli.main(["vocab", "suggest", "--home", h, "--projects", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["new"] == 2
    assert sorted(s["term"] for s in out["pending"]) == ["Chameleon CV", "Kestrel"]

    assert cli.main(["vocab", "seed", "--home", h, str(projects)]) == 0
    out = capsys.readouterr().out
    assert "0 new suggestion(s)" in out
    assert "From your projects" in out and "Chameleon CV" in out

    assert cli.main(["vocab", "dismiss", "--home", h, "Kestrel"]) == 0
    capsys.readouterr()
    assert cli.main(["vocab", "suggest", "--home", h]) == 0
    out = capsys.readouterr().out
    assert "0 new suggestion(s); 1 waiting" in out
    assert "Kestrel" not in out and "Chameleon CV" in out

    assert cli.main(["vocab", "seed", "--home", h, str(tmp_path / "missing")]) == 1
    assert "not a folder" in capsys.readouterr().err


def test_vocab_review_and_settings_open_the_window(tmp_path, monkeypatch):
    from shoutabout import ui_host

    opened = []

    class FakeHost:
        def __init__(self, services) -> None:
            self.svc = services

        def open_window(self, page="home"):
            opened.append((page, self.svc.paths.home))

        def wait(self):
            opened.append(("wait", None))

    monkeypatch.setattr(ui_host, "UiHost", FakeHost)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.main(["vocab", "review", "--home", str(tmp_path)]) == 0
    assert cli.main(["settings", "--home", str(tmp_path)]) == 0
    assert opened == [("teach", tmp_path), ("wait", None), ("settings", tmp_path), ("wait", None)]
