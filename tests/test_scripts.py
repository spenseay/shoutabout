"""Static and safe functional checks for scripts/*.ps1 (Windows PowerShell 5.1 scripts).

Never runs the installers. On Windows it uses powershell.exe to:
- parse every script with the PowerShell parser (zero errors) and read its comment-based help;
- dot-source scripts (which only loads their functions) to test pure helpers: token
  redaction, tunnel-service classification, install-state JSON round trip, config port
  helpers and the silence WAV writer.
Everywhere: scripts are pure ASCII (PS 5.1 reads BOM-less UTF-8 as ANSI) and contain no
hardcoded personal values (domains, private IPs, user profile paths, SSIDs).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "scripts"
EXPECTED = {
    "setup.ps1",
    "setup-admin.ps1",
    "setup-cloudflare.ps1",
    "uninstall.ps1",
    "make_test_audio.ps1",
    "test-routes.ps1",
}
PS_FILES = sorted(SCRIPTS_DIR.glob("*.ps1"))

ON_WINDOWS = sys.platform == "win32"
POWERSHELL = (shutil.which("powershell.exe") or shutil.which("powershell")) if ON_WINDOWS else None
needs_powershell = pytest.mark.skipif(
    POWERSHELL is None, reason="needs Windows PowerShell (powershell.exe)"
)

ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}
DOC_IP_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")  # RFC 5737 documentation ranges
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
USER_PROFILE_PATH = re.compile(r"[A-Za-z]:\\+Users\\+(?!Public\\)[^\\\s'\"]+", re.IGNORECASE)
FORBIDDEN = [
    (re.compile(r"shoutabout\.fyi", re.IGNORECASE), "personal domain"),
    (USER_PROFILE_PATH, "user profile path"),
    (re.compile(r"\bssid\b\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE), "hardcoded SSID"),
]
CURL_ALIAS = re.compile(r"(?im)^\s*(?:&\s*)?curl\s")
SC_ALIAS = re.compile(r"(?im)(^|[;|(]\s*)sc\s+(start|stop|query|delete|config|create)\b")


def _run_ps(script: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    """Run a PowerShell snippet via -EncodedCommand (no quoting problems)."""
    assert POWERSHELL is not None
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(REPO),
    )


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _last_json(stdout: str):
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, "no output from powershell"
    return json.loads(lines[-1])


# --------------------------------------------------------------------------- all platforms


def test_expected_scripts_exist():
    names = {p.name for p in PS_FILES}
    missing = EXPECTED - names
    assert not missing, f"missing scripts: {sorted(missing)}"


@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_script_is_pure_ascii(path: Path):
    data = path.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    if bad:
        line = data[: bad[0]].count(b"\n") + 1
        pytest.fail(f"{path.name}: {len(bad)} non-ASCII byte(s), first on line {line}")


@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_no_hardcoded_personal_values(path: Path):
    text = path.read_text("ascii", errors="replace")
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rx, what in FORBIDDEN:
            if rx.search(line):
                problems.append(f"line {lineno}: {what}: {line.strip()}")
        for ip in IPV4.findall(line):
            if ip in ALLOWED_IPS or ip.startswith(DOC_IP_PREFIXES):
                continue
            problems.append(f"line {lineno}: IP literal {ip}: {line.strip()}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_script_conventions(path: Path):
    text = path.read_text("ascii", errors="replace")
    assert "Set-StrictMode -Version Latest" in text
    assert "$ErrorActionPreference = 'Stop'" in text
    # PS 5.1 aliases that bite: curl -> Invoke-WebRequest, sc -> Set-Content.
    assert not CURL_ALIAS.search(text), "use curl.exe, not the curl alias"
    assert not SC_ALIAS.search(text), "use sc.exe, not the sc alias"


# --------------------------------------------------------------------------- Windows only

PARSE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = New-Object System.Collections.Generic.List[object]
foreach ($p in (Get-ChildItem -LiteralPath __DIR__ -Filter *.ps1 | Sort-Object Name)) {
    $tokens = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $p.FullName, [ref]$tokens, [ref]$errs)
    $errors = @($errs | ForEach-Object {
        'L{0}:{1} {2}' -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message })
    $help = $ast.GetHelpContent()
    $params = @()
    if ($ast.ParamBlock) {
        $params = @($ast.ParamBlock.Parameters | ForEach-Object {
            $_.Name.VariablePath.UserPath.ToLowerInvariant() })
    }
    $helpParams = @()
    $synopsis = ''; $description = ''; $examples = 0
    if ($help) {
        $synopsis = [string]$help.Synopsis
        $description = [string]$help.Description
        $examples = @($help.Examples).Count
        $helpParams = @($help.Parameters.Keys | ForEach-Object { ([string]$_).ToLowerInvariant() })
    }
    $out.Add([ordered]@{
        name = $p.Name; errors = $errors; synopsis = $synopsis.Trim()
        description = $description.Trim(); examples = $examples
        params = $params; help_params = $helpParams })
}
ConvertTo-Json -InputObject $out.ToArray() -Depth 5 -Compress
"""


@pytest.fixture(scope="module")
def parsed() -> dict[str, dict]:
    proc = _run_ps(PARSE_SCRIPT.replace("__DIR__", _ps_quote(SCRIPTS_DIR)))
    assert proc.returncode == 0, proc.stderr
    data = _last_json(proc.stdout)
    if isinstance(data, dict):
        data = [data]
    return {item["name"]: item for item in data}


@needs_powershell
@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_script_parses_without_errors(parsed, path: Path):
    info = parsed[path.name]
    assert info["errors"] == [], "\n".join(info["errors"])


@needs_powershell
@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_script_has_comment_based_help(parsed, path: Path):
    info = parsed[path.name]
    assert info["synopsis"], "missing .SYNOPSIS"
    assert info["description"], "missing .DESCRIPTION"
    assert info["examples"] >= 1, "missing .EXAMPLE"
    undocumented = sorted(set(info["params"]) - set(info["help_params"]))
    assert not undocumented, f"parameters without .PARAMETER help: {undocumented}"


CLOUDFLARE_HELPERS = r"""
$tok = 'eyJhIjoiMTIzNDU2Nzg5MCIsInQiOiJhYmNkZWYiLCJzIjoiWFlaIn0abcdefghijklmnop'
$exe = '"C:\Program Files (x86)\cloudflared\cloudflared.exe"'
$sysCfg = 'C:\Windows\System32\config\systemprofile\.cloudflared\config.yml'
$cases = [ordered]@{
  token      = $exe + ' tunnel run --token ' + $tok
  token_eq   = 'cloudflared.exe tunnel run --token=' + $tok
  token_file = $exe + ' tunnel run --token-file C:\secret\tunnel.token'
  config     = '"C:\Cloudflared\bin\cloudflared.exe" --config "' + $sysCfg + '" tunnel run'
  config_eq  = 'C:\Cloudflared\bin\cloudflared.exe --config=C:\cf\config.yml tunnel run'
  bare       = $exe
}
$res = [ordered]@{}
foreach ($k in $cases.Keys) {
  $kind = Get-TunnelKind $cases[$k]
  $res[$k] = [ordered]@{
    redacted = (Get-RedactedText $cases[$k]); kind = $kind.Kind; config = $kind.ConfigPath }
}
function Only([string]$v) { return [ordered]@{ redacted = $v; kind = ''; config = '' } }
$res['install_cmd'] = Only (Get-RedactedText ('cloudflared.exe service install ' + $tok))
$id = '6ff42ae2-765d-4adf-8112-31c55c1551ef'
$res['yaml'] = Only (New-LocalConfigYaml -Id $id -CredentialsFile ('C:\x\' + $id + '.json') `
    -Fqdn 'dictate.example.com' -HubPort 8766)
$res['host'] = Only (ConvertTo-Hostname 'HTTPS://Dictate.Example.com/health')
$bad = $false
try { [void](ConvertTo-Hostname 'not a host') } catch { $bad = $true }
$res['host_invalid_throws'] = Only ([string]$bad)
ConvertTo-Json -InputObject $res -Depth 5 -Compress
"""


@needs_powershell
def test_cloudflare_redaction_and_service_classification():
    script = f". {_ps_quote(SCRIPTS_DIR / 'setup-cloudflare.ps1')}\n" + CLOUDFLARE_HELPERS
    proc = _run_ps(script)
    assert proc.returncode == 0, proc.stderr
    res = _last_json(proc.stdout)
    for key in ("token", "token_eq", "token_file", "install_cmd"):
        red = res[key]["redacted"]
        assert "eyJ" not in red and "secret" not in red, (key, red)
        assert "<redacted>" in red, (key, red)
    assert res["token"]["kind"] == "remote"
    assert res["token_eq"]["kind"] == "remote"
    assert res["token_file"]["kind"] == "remote"
    assert res["config"]["kind"] == "local"
    assert res["config"]["config"].endswith(r"systemprofile\.cloudflared\config.yml")
    assert res["config_eq"]["config"] == r"C:\cf\config.yml"
    assert res["bare"]["kind"] == "local"
    yaml = res["yaml"]["redacted"]
    assert "tunnel: 6ff42ae2-765d-4adf-8112-31c55c1551ef" in yaml
    assert "  - hostname: dictate.example.com" in yaml
    assert "    service: http://127.0.0.1:8766" in yaml
    assert yaml.rstrip().endswith("- service: http_status:404")
    assert res["host"]["redacted"] == "dictate.example.com"
    assert res["host_invalid_throws"]["redacted"] == "True"


INSTALL_STATE = r"""
$s = Read-InstallState $p
$s['port'] = 8766
$s['public_hostname'] = 'dictate.example.com'
$s['created']['dns_route'] = $false
$s['cloudflared_files'] = @('C:\one.json')
$s['empty'] = @()
Write-InstallState -Path $p -State $s
$missing = Read-InstallState ($p + '.missing')
'{"missing_count": ' + $missing.Count + '}'
"""


@needs_powershell
def test_install_state_round_trip(tmp_path: Path):
    state = tmp_path / "install-state.json"
    state.write_text(
        json.dumps({"repo": "C:\\repo", "custom": 1, "created": {"tunnel": True}}), "utf-8"
    )
    script = f". {_ps_quote(SCRIPTS_DIR / 'setup.ps1')}\n$p = {_ps_quote(state)}\n" + INSTALL_STATE
    proc = _run_ps(script)
    assert proc.returncode == 0, proc.stderr
    assert _last_json(proc.stdout) == {"missing_count": 0}
    raw = state.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "install-state.json must not have a BOM"
    data = json.loads(raw.decode("utf-8"))
    assert data["repo"] == "C:\\repo"
    assert data["custom"] == 1
    assert data["port"] == 8766
    assert data["public_hostname"] == "dictate.example.com"
    assert data["created"] == {"tunnel": True, "dns_route": False}
    assert data["cloudflared_files"] == ["C:\\one.json"]
    assert data["empty"] == []
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", data["updated"])


CONFIG_PORT = r"""
$before = Get-ConfigPortFromFile $a
Set-ConfigPort -ConfigFile $a -NewPort 8766
Set-ConfigPort -ConfigFile $b -NewPort 9000
$r = [ordered]@{
    before = $before; after = (Get-ConfigPortFromFile $a); b = (Get-ConfigPortFromFile $b)
    none = (Get-ConfigPortFromFile ($a + '.missing')) }
ConvertTo-Json -InputObject $r -Compress
"""


@needs_powershell
def test_config_port_helpers(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '# comment\n[server]\nhost = "0.0.0.0"\nport = 8765\n\n[stt]\nmodel = "x"\n', "utf-8"
    )
    cfg2 = tmp_path / "config2.toml"
    cfg2.write_text('[server]\nhost = "0.0.0.0"\n\n[stt]\nport = 1\n', "utf-8")
    script = (
        f". {_ps_quote(SCRIPTS_DIR / 'setup.ps1')}\n"
        f"$a = {_ps_quote(cfg)}; $b = {_ps_quote(cfg2)}\n" + CONFIG_PORT
    )
    proc = _run_ps(script)
    assert proc.returncode == 0, proc.stderr
    assert _last_json(proc.stdout) == {"before": 8765, "after": 8766, "b": 9000, "none": None}
    assert 'model = "x"' in cfg.read_text("utf-8")
    assert "port = 1" in cfg2.read_text("utf-8")  # [stt] port untouched


@needs_powershell
def test_silence_wav_is_valid(tmp_path: Path):
    out = tmp_path / "silence.wav"
    script = (
        f". {_ps_quote(SCRIPTS_DIR / 'make_test_audio.ps1')}\n"
        f"Write-SilenceWav -Path {_ps_quote(out)} -Seconds 2.0\n"
        "'{\"ok\": true}'\n"
    )
    proc = _run_ps(script)
    assert proc.returncode == 0, proc.stderr
    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() == 32000
        frames = w.readframes(w.getnframes())
    samples = [
        int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)
    ]
    assert max(abs(s) for s in samples) <= 3
    assert any(samples), "near-silence should contain a little noise, not pure zeros"
