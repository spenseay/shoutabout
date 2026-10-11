<#
.SYNOPSIS
    Installs, exercises, upgrades and uninstalls ShoutAbout-Setup-<version>.exe silently and
    checks every change it makes to the machine. For CI and throwaway VMs only.

.DESCRIPTION
    Run elevated, in Windows PowerShell 5.1 (it uses System.Speech for a test sentence):

        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\smoke-test.ps1 `
            -Installer .\packaging\dist\ShoutAbout-Setup-0.1.0.exe -Version 0.1.0

    It refuses to run outside GitHub Actions unless -Force is given, because it really
    installs ShoutAbout, starts it, and deletes %APPDATA%\shoutabout at the end.

    Steps:
      1. Silent install (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART): files, uninstall entry,
         Start menu shortcut, firewall rule (inbound, TCP, Private only, ShoutAbout.exe),
         HKCU Run value, the app is NOT started by a silent install.
      2. ShoutAbout.exe --self-test (frozen bundle: imports, Tk, AAC decoding with Windows
         Media Foundation, no FFmpeg, VAD, cuBLAS, ...).
      3. CLI pass-through: --version and info --json.
      4. The installed app itself: starts the hub with a throwaway config (127.0.0.1, a high
         port, the small tiny.en model on the CPU), waits for /health, then transcribes a
         spoken test sentence through POST /transcribe (target=phone: nothing is pasted).
      5. Silent upgrade in place while the app runs: it is stopped, updated, started again.
      6. Silent uninstall: app stopped; files, firewall rule, Run value, shortcut and the
         uninstall entry are gone; user data in %APPDATA%\shoutabout is kept.
      7. Reinstall, then uninstall with /REMOVEUSERDATA: user data is removed too.

    Every ShoutAbout.exe started here (also the one the installer restarts) uses
    SHOUTABOUT_HOME = <WorkDir>\home, never the real config.

.PARAMETER Installer
    Path to ShoutAbout-Setup-<version>.exe.

.PARAMETER Version
    The version the installer must register (DisplayVersion).

.PARAMETER WorkDir
    Scratch folder for logs, the throwaway config and reports. Default: $env:RUNNER_TEMP or
    %TEMP%, plus shoutabout-smoke.

.PARAMETER Port
    Port for the test hub (loopback only). Default 18765.

.PARAMETER Force
    Run although this isn't GitHub Actions (a throwaway VM).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$WorkDir = '',
    [ValidateRange(1024, 65535)][int]$Port = 18765,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $Force -and $env:GITHUB_ACTIONS -ne 'true') {
    throw 'This script installs and uninstalls ShoutAbout and deletes %APPDATA%\shoutabout. Run it in CI or on a throwaway VM with -Force.'
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run elevated: the installer is per-machine.'
}

$Installer = (Resolve-Path -LiteralPath $Installer).Path
if (-not $WorkDir) {
    $base = $env:RUNNER_TEMP
    if (-not $base) { $base = $env:TEMP }
    $WorkDir = Join-Path $base 'shoutabout-smoke'
}
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$Logs = Join-Path $WorkDir 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

$AppGuid = '{B70711A6-7F23-457D-B910-31004776AB13}'
$AppDir = Join-Path $env:ProgramFiles 'ShoutAbout'
$Exe = Join-Path $AppDir 'ShoutAbout.exe'
$UninstallKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{0}_is1' -f $AppGuid
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Shortcut = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\ShoutAbout.lnk'
$UserData = Join-Path $env:APPDATA 'shoutabout'
$TestHome = Join-Path $WorkDir 'home'
$env:SHOUTABOUT_HOME = $TestHome

$script:Failures = New-Object System.Collections.Generic.List[string]
$script:Passed = 0

function Write-Step([string]$Text) { Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan }

function Assert-True([bool]$Condition, [string]$What) {
    if ($Condition) {
        $script:Passed++
        Write-Host ('  PASS  {0}' -f $What) -ForegroundColor Green
    } else {
        $script:Failures.Add($What)
        Write-Host ('  FAIL  {0}' -f $What) -ForegroundColor Red
    }
}

function Stop-OnFailure {
    if ($script:Failures.Count -gt 0) {
        throw ('{0} check(s) failed: {1}' -f $script:Failures.Count, ($script:Failures -join '; '))
    }
}

function Invoke-Setup([string]$Exe, [string[]]$Arguments, [string]$LogName) {
    $log = Join-Path $Logs $LogName
    $allArgs = @($Arguments) + @(('/LOG="{0}"' -f $log))
    $started = Get-Date
    # Not Start-Process -Wait: that also waits for every descendant, and a silent upgrade
    # starts ShoutAbout again, which keeps running.
    $p = Start-Process -FilePath $Exe -ArgumentList $allArgs -PassThru
    $null = $p.Handle  # keeps ExitCode readable after the process exits (PowerShell 5.1)
    if (-not $p.WaitForExit(600000)) { throw ('{0} did not finish within 10 minutes' -f (Split-Path -Leaf $Exe)) }
    Write-Host ('  {0} {1} -> exit {2} in {3:N0} s' -f (Split-Path -Leaf $Exe), ($Arguments -join ' '), $p.ExitCode, ((Get-Date) - $started).TotalSeconds)
    return $p.ExitCode
}

function Get-PeSubsystem([string]$Path) {
    # IMAGE_OPTIONAL_HEADER.Subsystem: 2 = Windows GUI, 3 = console.
    $bytes = [IO.File]::ReadAllBytes($Path)
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    return [BitConverter]::ToUInt16($bytes, $pe + 0x5C)
}

function Get-AppProcesses { return @(Get-Process -Name 'ShoutAbout' -ErrorAction SilentlyContinue) }

function Get-AppFirewallRules { return @(Get-NetFirewallRule -DisplayName 'ShoutAbout' -ErrorAction SilentlyContinue) }

function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Host ('  .. gave up waiting for {0} after {1} s' -f $What, $Seconds)
    return [bool](& $Condition)
}

function Test-Installed {
    Write-Step 'Install checks'
    Assert-True (Test-Path -LiteralPath $Exe) "ShoutAbout.exe is in $AppDir"
    Assert-True (Test-Path -LiteralPath (Join-Path $AppDir '_internal\nvidia\cublas\bin\cublas64_12.dll')) 'cuBLAS is bundled'
    Assert-True (Test-Path -LiteralPath (Join-Path $AppDir 'THIRD-PARTY-NOTICES.txt')) 'THIRD-PARTY-NOTICES.txt is installed'
    $asio = @(Get-ChildItem -LiteralPath $AppDir -Recurse -File -Force | Where-Object { $_.Name -like '*asio*' })
    Assert-True ($asio.Count -eq 0) ('no ASIO files were installed (found {0})' -f (($asio | ForEach-Object { $_.FullName }) -join ', '))
    # PyAV's FFmpeg build links x264/x265 (GPL): none of it may ship (packaging/bundle_rules.py).
    $ffmpegPatterns = @('avcodec*', 'avformat*', 'avutil*', 'avdevice*', 'avfilter*', 'swresample*', 'swscale*', '*x264*', '*x265*')
    $ffmpeg = @(Get-ChildItem -LiteralPath $AppDir -Recurse -Force | Where-Object {
            $item = $_
            ($item.PSIsContainer -and @('av', 'av.libs') -contains $item.Name) -or
            (-not $item.PSIsContainer -and @($ffmpegPatterns | Where-Object { $item.Name -like $_ }).Count -gt 0)
        })
    Assert-True ($ffmpeg.Count -eq 0) ('no PyAV/FFmpeg files were installed (found {0})' -f (($ffmpeg | ForEach-Object { $_.FullName }) -join ', '))
    Assert-True ((Get-PeSubsystem $Exe) -eq 2) 'ShoutAbout.exe is a windowed program (no console window)'
    Assert-True (Test-Path -LiteralPath $Shortcut) 'Start menu shortcut exists'
    $entry = Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction SilentlyContinue
    Assert-True ($null -ne $entry) 'uninstall entry exists'
    if ($null -ne $entry) {
        Assert-True ($entry.DisplayVersion -eq $Version) ('DisplayVersion is {0} (found {1})' -f $Version, $entry.DisplayVersion)
        Assert-True ([string]$entry.UninstallString -like '*unins000.exe" /SILENT') ('UninstallString opens our own dialog: {0}' -f $entry.UninstallString)
    }
    $rules = @(Get-AppFirewallRules)
    Assert-True ($rules.Count -eq 1) ('exactly one firewall rule named ShoutAbout (found {0})' -f $rules.Count)
    if ($rules.Count -ge 1) {
        $r = $rules[0]
        $app = $r | Get-NetFirewallApplicationFilter
        $ports = $r | Get-NetFirewallPortFilter
        Assert-True ([string]$r.Direction -eq 'Inbound') 'firewall rule is inbound'
        Assert-True ([string]$r.Action -eq 'Allow') 'firewall rule allows'
        Assert-True ([string]$r.Enabled -eq 'True') 'firewall rule is enabled'
        Assert-True ([string]$r.Profile -eq 'Private') ('firewall rule is Private only (found {0})' -f $r.Profile)
        Assert-True ([string]$ports.Protocol -eq 'TCP') 'firewall rule is TCP'
        Assert-True ($app.Program -eq $Exe) ('firewall rule is for {0} (found {1})' -f $Exe, $app.Program)
    }
}

function Write-SpeechWav([string]$Path, [string]$Text) {
    try {
        Add-Type -AssemblyName System.Speech
        $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
        try {
            $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
            $synth.SetOutputToWaveFile($Path, $format)
            $synth.Speak($Text)
        } finally { $synth.Dispose() }
        return ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt 10000)
    } catch {
        Write-Host ('  .. no text-to-speech here ({0}); using a tone' -f $_.Exception.Message)
        return $false
    }
}

function Write-ToneWav([string]$Path) {
    $rate = 16000; $n = $rate
    $stream = [IO.File]::Create($Path)
    $w = New-Object IO.BinaryWriter($stream)
    try {
        $w.Write([Text.Encoding]::ASCII.GetBytes('RIFF')); $w.Write([int](36 + 2 * $n))
        $w.Write([Text.Encoding]::ASCII.GetBytes('WAVEfmt ')); $w.Write([int]16); $w.Write([int16]1); $w.Write([int16]1)
        $w.Write([int]$rate); $w.Write([int](2 * $rate)); $w.Write([int16]2); $w.Write([int16]16)
        $w.Write([Text.Encoding]::ASCII.GetBytes('data')); $w.Write([int](2 * $n))
        for ($i = 0; $i -lt $n; $i++) { $w.Write([int16](8000 * [Math]::Sin(2 * [Math]::PI * 440 * $i / $rate))) }
    } finally { $w.Dispose() }
}

function Invoke-Hub([string]$Method, [string]$Path, [string]$Token = '', [string]$File = '') {
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds(120)
    try {
        $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::new($Method), ('http://127.0.0.1:{0}{1}' -f $Port, $Path))
        if ($Token) { $req.Headers.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', $Token) }
        if ($File) {
            $content = New-Object System.Net.Http.ByteArrayContent(, [IO.File]::ReadAllBytes($File))
            $content.Headers.ContentType = New-Object System.Net.Http.Headers.MediaTypeHeaderValue('audio/wav')
            $req.Content = $content
        }
        $resp = $client.SendAsync($req).GetAwaiter().GetResult()
        $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = $body }
    } catch {
        return [pscustomobject]@{ Status = 0; Body = $_.Exception.Message }
    } finally { $client.Dispose() }
}

function Test-HubHealthy { return ((Invoke-Hub 'GET' '/health').Status -eq 200) }

# ------------------------------------------------------------------------------ 1. install
Write-Step ('Install {0}' -f (Split-Path -Leaf $Installer))
Assert-True ($null -eq (Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction SilentlyContinue)) 'not installed before the test'
$code = Invoke-Setup $Installer @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 'install-1.log'
Assert-True ($code -eq 0) 'installer exit code 0'
Test-Installed
$run = Get-ItemProperty -LiteralPath $RunKey -Name 'ShoutAbout' -ErrorAction SilentlyContinue
Assert-True ($null -ne $run -and $run.ShoutAbout -eq ('"{0}" run' -f $Exe)) ('Run value starts ShoutAbout at sign-in (found {0})' -f $(if ($run) { $run.ShoutAbout } else { 'nothing' }))
Assert-True (@(Get-AppProcesses).Count -eq 0) 'a silent install does not start the app'
Stop-OnFailure

# ------------------------------------------------------------------------------ 2. self-test
Write-Step 'ShoutAbout.exe --self-test'
$report = Join-Path $WorkDir 'selftest.json'
$p = Start-Process -FilePath $Exe -ArgumentList @('--self-test', '--home', ('"{0}"' -f (Join-Path $WorkDir 'selftest-home')), '--out', ('"{0}"' -f $report)) -Wait -PassThru
Assert-True ($p.ExitCode -eq 0) ('self-test exit code 0 (got {0})' -f $p.ExitCode)
if (Test-Path -LiteralPath $report) {
    $json = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    foreach ($c in $json.checks) { Write-Host ('  {0,-5} {1}: {2}' -f $c.status.ToUpper(), $c.name, $c.detail) }
    Assert-True ([bool]$json.ok) 'self-test ok'
    Assert-True ([bool]$json.frozen) 'self-test ran the frozen app'
    $cublas = @($json.checks | Where-Object { $_.name -eq 'cublas' })
    Assert-True ($cublas.Count -eq 1 -and $cublas[0].status -eq 'pass') 'cuBLAS loads from the bundle'
    $audio = @($json.checks | Where-Object { $_.name -eq 'audio' })
    Assert-True ($audio.Count -eq 1 -and $audio[0].status -eq 'pass' -and [string]$audio[0].detail -like '*Media Foundation*') 'AAC .m4a decodes with Windows Media Foundation'
    $noFfmpeg = @($json.checks | Where-Object { $_.name -eq 'no-ffmpeg' })
    Assert-True ($noFfmpeg.Count -eq 1 -and $noFfmpeg[0].status -eq 'pass') 'the self-test finds no PyAV/FFmpeg in the app'
} else {
    Assert-True $false 'self-test wrote its report'
}
Stop-OnFailure

# ------------------------------------------------------------------------------ 3. CLI
Write-Step 'CLI pass-through'
$out = Join-Path $WorkDir 'cli-out.txt'
$err = Join-Path $WorkDir 'cli-err.txt'
$p = Start-Process -FilePath $Exe -ArgumentList '--version' -RedirectStandardOutput $out -RedirectStandardError $err -Wait -PassThru -NoNewWindow
Assert-True ($p.ExitCode -eq 0 -and (Get-Content -LiteralPath $out -Raw).Trim() -eq ('shoutabout {0}' -f $Version)) ('--version prints shoutabout {0}' -f $Version)
$p = Start-Process -FilePath $Exe -ArgumentList @('info', '--json') -RedirectStandardOutput $out -RedirectStandardError $err -Wait -PassThru -NoNewWindow
$info = $null
try { $info = Get-Content -LiteralPath $out -Raw | ConvertFrom-Json } catch { }
Assert-True ($p.ExitCode -eq 0 -and $null -ne $info -and $info.home -eq $TestHome) 'info --json works and uses SHOUTABOUT_HOME'

# ------------------------------------------------------------------------------ 4. the app
Write-Step 'Run the installed app (hub + tray) with a throwaway config'
New-Item -ItemType Directory -Force -Path $TestHome | Out-Null
$models = (Join-Path $WorkDir 'models').Replace('\', '/')
$config = @"
# Written by packaging/smoke-test.ps1: loopback only, small CPU model, no LLM.
[server]
host = "127.0.0.1"
port = $Port

[stt]
model = "tiny.en"
device = "cpu"
download_root = "$models"

[cleanup]
enabled = false

[learn]
enabled = false
"@
Set-Content -LiteralPath (Join-Path $TestHome 'config.toml') -Value $config -Encoding ASCII
# Start it exactly the way Windows does at sign-in: the command line in the Run value
# ("<exe>" run). The silent upgrade below restarts it without arguments (the other way).
$runValue = [string](Get-ItemProperty -LiteralPath $RunKey -Name 'ShoutAbout').ShoutAbout
if ($runValue -notmatch '^"([^"]+)"\s*(.*)$') { throw ('unexpected Run value: {0}' -f $runValue) }
$runExe = $Matches[1]
$runArgs = $Matches[2]
Assert-True ($runExe -eq $Exe -and $runArgs -eq 'run') ('the Run value is "<exe>" run (found {0})' -f $runValue)
$app = Start-Process -FilePath $runExe -ArgumentList $runArgs -PassThru
Write-Host ('  started {0} {1} (pid {2})' -f $runExe, $runArgs, $app.Id)
$healthy = Wait-Until { Test-HubHealthy } 90 '/health'
Assert-True $healthy ('/health answers on 127.0.0.1:{0}' -f $Port)
if (-not $healthy) {
    Get-Content -LiteralPath (Join-Path $TestHome 'logs\hub.log') -Tail 40 -ErrorAction SilentlyContinue | Write-Host
}
$token = (Get-Content -LiteralPath (Join-Path $TestHome 'hub_token.txt') -Raw).Trim()
$wav = Join-Path $WorkDir 'speech.wav'
$spoken = Write-SpeechWav $wav 'Testing the ShoutAbout installer on a build machine.'
if (-not $spoken) { Write-ToneWav $wav }
$script:Result = $null
$ready = Wait-Until {
    $script:Result = Invoke-Hub 'POST' '/transcribe?target=phone' $token $wav
    return ($script:Result.Status -ne 503 -and $script:Result.Status -ne 0)
} 300 'the speech engine (tiny.en download + load)'
Write-Host ('  /transcribe -> {0} {1}' -f $script:Result.Status, $script:Result.Body)
Assert-True ($ready -and $script:Result.Status -eq 200) 'POST /transcribe returns 200'
if ($script:Result.Status -eq 200) {
    $body = $script:Result.Body | ConvertFrom-Json
    Assert-True ($body.pasted -eq $false) 'target=phone never pastes'
    if ($spoken) {
        Assert-True ([string]$body.text -match 'install') ('the spoken sentence was transcribed: "{0}"' -f $body.text)
    }
}
Get-ChildItem -LiteralPath (Join-Path $TestHome 'logs') -ErrorAction SilentlyContinue | Copy-Item -Destination $Logs -ErrorAction SilentlyContinue
Stop-OnFailure

# ------------------------------------------------------------------------------ 5. upgrade
Write-Step 'Upgrade in place while the app is running'
$before = @(Get-AppProcesses | ForEach-Object { $_.Id })
Assert-True ($before.Count -ge 1) 'the app is running before the upgrade'
$code = Invoke-Setup $Installer @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 'install-2-upgrade.log'
Assert-True ($code -eq 0) 'upgrade exit code 0'
Assert-True (@(Get-AppProcesses | Where-Object { $before -contains $_.Id }).Count -eq 0) 'the running app was stopped for the upgrade'
Test-Installed
Assert-True (Wait-Until { Test-HubHealthy } 90 '/health after the upgrade') 'the app was started again after the silent upgrade'
Stop-OnFailure

# ------------------------------------------------------------------------------ 6. uninstall
Write-Step 'Uninstall (keep user data)'
New-Item -ItemType Directory -Force -Path $UserData | Out-Null
Set-Content -LiteralPath (Join-Path $UserData 'smoke-marker.txt') -Value 'keep me' -Encoding ASCII
$uninstaller = Join-Path $AppDir 'unins000.exe'
$code = Invoke-Setup $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 'uninstall-1.log'
Assert-True ($code -eq 0) 'uninstaller exit code 0'
Assert-True (Wait-Until { -not (Test-Path -LiteralPath $UninstallKey) -and -not (Test-Path -LiteralPath $AppDir) } 180 'the uninstaller') 'files and uninstall entry removed'
Assert-True (@(Get-AppProcesses).Count -eq 0) 'the app was stopped'
Assert-True (@(Get-AppFirewallRules).Count -eq 0) 'firewall rule removed'
Assert-True ($null -eq (Get-ItemProperty -LiteralPath $RunKey -Name 'ShoutAbout' -ErrorAction SilentlyContinue)) 'Run value removed'
Assert-True (-not (Test-Path -LiteralPath $Shortcut)) 'Start menu shortcut removed'
Assert-True (-not (Test-Path -LiteralPath $AppDir)) "$AppDir removed"
Assert-True (Test-Path -LiteralPath (Join-Path $UserData 'smoke-marker.txt')) 'user data kept by default'
Stop-OnFailure

# ------------------------------------------------------------------------------ 7. remove data
Write-Step 'Reinstall, then uninstall with /REMOVEUSERDATA'
$code = Invoke-Setup $Installer @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 'install-3.log'
Assert-True ($code -eq 0) 'reinstall exit code 0'
$code = Invoke-Setup $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/REMOVEUSERDATA') 'uninstall-2.log'
Assert-True ($code -eq 0) 'uninstaller exit code 0'
Assert-True (Wait-Until { -not (Test-Path -LiteralPath $UninstallKey) -and -not (Test-Path -LiteralPath $AppDir) } 180 'the uninstaller') 'uninstalled again'
Assert-True (-not (Test-Path -LiteralPath $UserData)) 'user data removed with /REMOVEUSERDATA'
Stop-OnFailure

Write-Host ''
Write-Host ('All {0} checks passed.' -f $script:Passed) -ForegroundColor Green
