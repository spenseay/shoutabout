<#
.SYNOPSIS
    Installs and configures the ShoutAbout hub on this Windows PC (safe to re-run).

.DESCRIPTION
    Run from a NORMAL (not elevated) PowerShell window in the repository root:

        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1

    Steps:
       1. Prerequisites: Windows 10/11, winget, NVIDIA driver (nvidia-smi), repo layout.
       2. uv (Python package manager): installed with winget if missing.
       3. Python 3.12 + dependencies into .venv: uv sync --locked --extra cuda --python 3.12
          (includes the ~550 MB NVIDIA cuBLAS wheel; uv downloads Python 3.12 if needed).
       4. Config dir (%APPDATA%\shoutabout): config.toml, vocab.txt, hub_token.txt (python -m shoutabout init).
       5. Whisper model download (~1.6 GB, large-v3-turbo).
       6. Ollama (winget) + the cleanup model from config.toml (~2.5 GB).
       7. "ShoutAbout Hub" scheduled task (at logon, as you, not elevated) and Windows Firewall
          rules (ONE UAC prompt, for the firewall only).
       8. Cloudflare Tunnel: scripts\setup-cloudflare.ps1.
       9. install-state.json in the config dir (what was installed; read by doctor/uninstall).
      10. Starts the hub (scheduled task) and waits for /health.
      11. python -m shoutabout doctor, network profile check, and a summary.

    The hub token is never printed. To move it to the iPhone run:
        .\.venv\Scripts\python.exe -m shoutabout token --qr

    If the repository was downloaded as a ZIP and PowerShell refuses to run the scripts:
        Get-ChildItem -Recurse .\scripts | Unblock-File

.PARAMETER PublicHostname
    Public hostname for the Cloudflare Tunnel route, e.g. dictate.example.com. Passed to
    setup-cloudflare.ps1 (which asks for it if omitted) and saved in install-state.json.

.PARAMETER Port
    Hub TCP port. Default: the port already in config.toml (8765 for a new install). If given,
    it is written to config.toml ("python -m shoutabout init --port N") and used for the
    firewall rules, the tunnel service URL and install-state.json. Setup stops early if another
    program already listens on the port.

.PARAMETER SkipCloudflare
    Do not run setup-cloudflare.ps1 (LAN only, or set the tunnel up later).

.PARAMETER SkipOllama
    Do not install Ollama or pull the cleanup model.

.PARAMETER SkipModel
    Do not download the Whisper model now (the hub downloads it on first start).

.PARAMETER SkipFirewall
    Do not create the firewall rules (no UAC prompt). The iPhone cannot reach the hub over the
    LAN without them.

.PARAMETER SkipTask
    Do not register the "ShoutAbout Hub" scheduled task (the hub will not start at logon).

.PARAMETER NoStart
    Do not (re)start the hub at the end.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -PublicHostname dictate.example.com

.EXAMPLE
    # LAN only, no Cloudflare, no LLM cleanup:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -SkipCloudflare -SkipOllama
#>
[CmdletBinding()]
param(
    [string]$PublicHostname = '',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$SkipCloudflare,
    [switch]$SkipOllama,
    [switch]$SkipModel,
    [switch]$SkipFirewall,
    [switch]$SkipTask,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue } catch { }

$script:PortGiven = $PSBoundParameters.ContainsKey('Port')
$script:TaskName = 'ShoutAbout Hub'
$script:Warnings = New-Object System.Collections.Generic.List[string]
$script:StepNo = 0
$script:StepTotal = 11
$script:LastExit = 0
$script:ExitCode = 0
$script:HaveWinget = $false
$script:SelfDir = $PSScriptRoot

# ------------------------------------------------------------------------------ output helpers
function Write-Step([string]$Title) {
    $script:StepNo++
    Write-Host ''
    Write-Host ('[{0}/{1}] {2}' -f $script:StepNo, $script:StepTotal, $Title) -ForegroundColor Cyan
}
function Write-Ok([string]$Text) { Write-Host ('  OK    {0}' -f $Text) -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host ('  ..    {0}' -f $Text) }
function Write-Skip([string]$Text) { Write-Host ('  SKIP  {0}' -f $Text) -ForegroundColor DarkGray }
function Write-Warn([string]$Text) {
    Write-Host ('  WARN  {0}' -f $Text) -ForegroundColor Yellow
    $script:Warnings.Add($Text)
}

# ------------------------------------------------------------------------------ generic helpers
function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Exe {
    # Runs a native program with its output going to the console. Sets $script:LastExit.
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$Arguments = @(), [switch]$AllowFailure)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & $FilePath @Arguments | Out-Host
    $script:LastExit = $LASTEXITCODE
    if ($script:LastExit -ne 0 -and -not $AllowFailure) {
        throw ('"{0} {1}" failed with exit code {2}.' -f (Split-Path -Leaf $FilePath), ($Arguments -join ' '), $script:LastExit)
    }
}

function Invoke-ExeCapture {
    # Runs a native program and returns @{ExitCode; StdOut; StdErr} (stderr kept separate).
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$Arguments = @(), [switch]$Utf8)
    $ErrorActionPreference = 'Continue'
    $out = New-Object System.Collections.Generic.List[string]
    $err = New-Object System.Collections.Generic.List[string]
    $code = -1
    $prevEnc = $null
    $prevPy = $env:PYTHONIOENCODING
    if ($Utf8) {
        try { $prevEnc = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { $prevEnc = $null }
        $env:PYTHONIOENCODING = 'utf-8'
    }
    try {
        $global:LASTEXITCODE = 0
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $err.Add($_.ToString()) } else { $out.Add([string]$_) }
        }
        $code = $LASTEXITCODE
    } catch {
        $err.Add($_.Exception.Message)
    } finally {
        if ($Utf8) {
            if ($null -ne $prevEnc) { try { [Console]::OutputEncoding = $prevEnc } catch { } }
            $env:PYTHONIOENCODING = $prevPy
        }
    }
    return [pscustomobject]@{ ExitCode = $code; StdOut = $out.ToArray(); StdErr = $err.ToArray() }
}

function Update-SessionPath {
    # Re-read PATH from the registry (after winget installs) and add per-user tool folders.
    $parts = New-Object System.Collections.Generic.List[string]
    $sources = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama'),
        $env:Path
    )
    foreach ($s in $sources) {
        if (-not $s) { continue }
        foreach ($p in $s.Split(';')) {
            $q = $p.Trim()
            if ($q -and -not $parts.Contains($q)) { $parts.Add($q) }
        }
    }
    $env:Path = $parts -join ';'
}

function Get-Prop {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name) -and $null -ne $Object[$Name]) { return $Object[$Name] }
        return $Default
    }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p -or $null -eq $p.Value) { return $Default }
    return $p.Value
}

function ConvertTo-OrderedHashtable {
    param($InputObject)
    if ($null -eq $InputObject) { return $null }
    if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
        $h = [ordered]@{}
        foreach ($p in $InputObject.PSObject.Properties) { $h[$p.Name] = ConvertTo-OrderedHashtable $p.Value }
        return $h
    }
    if ($InputObject -is [System.Collections.IDictionary]) {
        $h = [ordered]@{}
        foreach ($k in @($InputObject.Keys)) { $h[[string]$k] = ConvertTo-OrderedHashtable $InputObject[$k] }
        return $h
    }
    if ($InputObject -is [System.Collections.IEnumerable] -and $InputObject -isnot [string]) {
        $list = New-Object System.Collections.Generic.List[object]
        foreach ($item in $InputObject) { $list.Add((ConvertTo-OrderedHashtable $item)) }
        return , $list.ToArray()
    }
    return $InputObject
}

function Get-ConfigHome {
    # Mirrors shoutabout.config.default_home() on Windows.
    if ($env:SHOUTABOUT_HOME) { return [Environment]::ExpandEnvironmentVariables($env:SHOUTABOUT_HOME) }
    return (Join-Path $env:APPDATA 'shoutabout')
}

function Read-InstallState([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return [ordered]@{} }
    try {
        $raw = [IO.File]::ReadAllText($Path)
        if ($raw.Trim()) {
            $h = ConvertTo-OrderedHashtable ($raw | ConvertFrom-Json)
            if ($h -is [System.Collections.IDictionary]) { return $h }
        }
    } catch {
        Write-Warn ('{0} is not valid JSON; it will be rewritten.' -f $Path)
    }
    return [ordered]@{}
}

function Write-InstallState([string]$Path, [System.Collections.IDictionary]$State) {
    $State['updated'] = (Get-Date).ToString("yyyy-MM-dd'T'HH:mm:sszzz")
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $json = ConvertTo-Json -InputObject $State -Depth 10
    [IO.File]::WriteAllText($Path, $json + "`n", (New-Object System.Text.UTF8Encoding $false))
}

function ConvertTo-Hostname([string]$Value) {
    # "https://Dictate.Example.com/" -> "dictate.example.com"; '' if empty; throws if invalid.
    $v = ([string]$Value).Trim().ToLowerInvariant()
    if (-not $v) { return '' }
    $v = $v -replace '^[a-z]+://', ''
    $v = ($v -split '[/?#]')[0]
    $v = $v.TrimEnd('.')
    if ($v -notmatch '^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$') {
        throw ('"{0}" is not a valid hostname (expected something like dictate.example.com).' -f $Value)
    }
    return $v
}

function Get-PowerShellExe {
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        return (Join-Path $env:windir 'Sysnative\WindowsPowerShell\v1.0\powershell.exe')
    }
    return (Join-Path $env:windir 'System32\WindowsPowerShell\v1.0\powershell.exe')
}

function Get-DefaultRouteProfile {
    # Network profile (Public/Private) of the interface that carries the default IPv4 route.
    try {
        $routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop)
        if ($routes.Count -eq 0) { return $null }
        $best = $null
        $bestMetric = [int]::MaxValue
        foreach ($r in $routes) {
            $ifMetric = 0
            $ipIf = Get-NetIPInterface -InterfaceIndex $r.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -ne $ipIf) { $ifMetric = [int]$ipIf.InterfaceMetric }
            $m = [int]$r.RouteMetric + $ifMetric
            if ($m -lt $bestMetric) { $bestMetric = $m; $best = $r }
        }
        if ($null -eq $best) { return $null }
        $prof = Get-NetConnectionProfile -InterfaceIndex $best.ifIndex -ErrorAction Stop | Select-Object -First 1
        if ($null -eq $prof) { return $null }
        return [pscustomobject]@{
            Name           = [string]$prof.Name
            InterfaceAlias = [string]$prof.InterfaceAlias
            Category       = [string]$prof.NetworkCategory
        }
    } catch {
        return $null
    }
}

# ------------------------------------------------------------------------------ hub helpers
function Get-HubInfo([string]$Python) {
    $r = Invoke-ExeCapture -FilePath $Python -Arguments @('-m', 'shoutabout', 'info', '--json') -Utf8
    if ($r.ExitCode -ne 0) {
        $detail = (@($r.StdErr) + @($r.StdOut) | Select-Object -Last 5) -join ' | '
        throw ('"python -m shoutabout info --json" failed (exit {0}): {1}' -f $r.ExitCode, $detail)
    }
    $text = (@($r.StdOut) -join "`n").Trim()
    try { return ($text | ConvertFrom-Json) } catch { throw ('"info --json" did not print JSON: {0}' -f $text) }
}

function Get-HubProcesses {
    $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+shoutabout\s+run' })
    return $procs
}

function Stop-Hub {
    $stopped = $false
    try {
        $t = Get-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction SilentlyContinue
        if ($null -ne $t -and [string]$t.State -eq 'Running') { Stop-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction Stop; $stopped = $true }
    } catch { }
    foreach ($p in @(Get-HubProcesses)) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped = $true } catch { }
    }
    if ($stopped) {
        for ($i = 0; $i -lt 20; $i++) {
            if (@(Get-HubProcesses).Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        Write-Info 'Stopped the running hub.'
    }
}

function Wait-HubHealth([int]$HubPort, [int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/health' -f $HubPort) -TimeoutSec 3 -UseBasicParsing
            if ($null -ne $r -and (Get-Prop $r 'ok' $false)) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-ConfigPortFromFile([string]$ConfigFile) {
    # [server] port from an existing config.toml, or $null (used before the venv exists).
    if (-not (Test-Path -LiteralPath $ConfigFile)) { return $null }
    $inServer = $false
    foreach ($l in [IO.File]::ReadAllLines($ConfigFile)) {
        if ($l -match '^\s*\[([^\]]+)\]') { $inServer = ($Matches[1].Trim() -eq 'server'); continue }
        if ($inServer -and $l -match '^\s*port\s*=\s*(\d+)') { return [int]$Matches[1] }
    }
    return $null
}

function Get-PortListeners([int]$LocalPort) {
    # Processes listening on a TCP port: @{ProcessId; Name; CommandLine}.
    $found = New-Object System.Collections.Generic.List[object]
    $conns = @(Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)
    foreach ($procId in @($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $cim = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId={0}' -f [int]$procId) -ErrorAction SilentlyContinue
        $name = ''; $cmd = ''
        if ($null -ne $cim) { $name = [string]$cim.Name; $cmd = [string]$cim.CommandLine }
        $found.Add([pscustomobject]@{ ProcessId = [int]$procId; Name = $name; CommandLine = $cmd })
    }
    return $found.ToArray()
}

function Get-ForeignPortListeners([int]$LocalPort) {
    # Listeners that are NOT the ShoutAbout hub.
    return @(Get-PortListeners $LocalPort | Where-Object { $_.CommandLine -notmatch '-m\s+shoutabout\s+run' })
}

function Assert-PortFree([int]$LocalPort) {
    $others = @(Get-ForeignPortListeners $LocalPort)
    if ($others.Count -eq 0) { Write-Ok ('TCP port {0} is free (or used by the ShoutAbout hub).' -f $LocalPort); return }
    $suggest = $LocalPort + 1
    while ($suggest -lt 65535 -and @(Get-PortListeners $suggest).Count -gt 0) { $suggest++ }
    foreach ($o in $others) {
        $cmd = if ($o.CommandLine) { $o.CommandLine } else { '(command line not visible)' }
        Write-Host ('  Port {0} is used by PID {1} {2}: {3}' -f $LocalPort, $o.ProcessId, $o.Name, $cmd) -ForegroundColor Red
    }
    $first = $others[0]
    throw ('Port {0} is in use by {1} (PID {2}). Either stop that program or re-run with -Port {3} (any free port): powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Port {3}' -f $LocalPort, $first.Name, $first.ProcessId, $suggest)
}

function Set-ConfigPort([string]$ConfigFile, [int]$NewPort) {
    # Fallback only, for a CLI without "init --port".
    # Sets [server] port = N in config.toml, keeping everything else (comments included).
    $text = [IO.File]::ReadAllText($ConfigFile)
    $nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.AddRange([string[]]($text -split '\r?\n'))
    $inServer = $false; $serverAt = -1; $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $l = $lines[$i]
        if ($l -match '^\s*\[([^\]]+)\]\s*(#.*)?$') {
            if ($inServer -and -not $done) { $lines.Insert($i, ('port = {0}' -f $NewPort)); $done = $true; break }
            $inServer = ($Matches[1].Trim() -eq 'server')
            if ($inServer) { $serverAt = $i }
            continue
        }
        if ($inServer -and $l -match '^\s*port\s*=') { $lines[$i] = ('port = {0}' -f $NewPort); $done = $true; break }
    }
    if (-not $done) {
        if ($serverAt -ge 0) { $lines.Add(('port = {0}' -f $NewPort)) }
        else { $lines.Add(''); $lines.Add('[server]'); $lines.Add(('port = {0}' -f $NewPort)) }
    }
    [IO.File]::WriteAllText($ConfigFile, ($lines -join $nl), (New-Object System.Text.UTF8Encoding $false))
}

function Register-HubTask([string]$RepoRoot, [string]$UserId) {
    # Registers the logon task as the current (non-elevated) user, so the user owns it.
    $pythonw = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m shoutabout run' -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $trigger.Delay = 'PT15S'   # give Wi-Fi a moment after logon
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    $settings.Priority = 5     # default 7 is "background" (below-normal CPU and I/O priority)
    Register-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force `
        -Description 'ShoutAbout dictation hub (python -m shoutabout run). Created by scripts\setup.ps1.' | Out-Null
}

function Get-PythonBlockRules {
    # Read-only (works without admin): enabled inbound Block rules for python*.exe.
    $found = New-Object System.Collections.Generic.List[string]
    try {
        $rules = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
                Where-Object { $_.Program -like '*\python*.exe' } |
                Get-NetFirewallRule -ErrorAction SilentlyContinue |
                Where-Object { [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Block' -and [string]$_.Enabled -eq 'True' })
        foreach ($r in $rules) { $found.Add(('{0} (Remove-NetFirewallRule -Name ''{1}'')' -f $r.DisplayName, ([string]$r.Name).Replace("'", "''"))) }
    } catch { }
    return $found.ToArray()
}

# ------------------------------------------------------------------------------ installers
function Invoke-WingetInstall([string]$Id) {
    if (-not $script:HaveWinget) { throw ('winget is not available, so {0} cannot be installed automatically.' -f $Id) }
    Write-Info ('winget install --id {0} -e' -f $Id)
    Invoke-Exe -FilePath 'winget.exe' -Arguments @('install', '--id', $Id, '-e', '--accept-source-agreements', '--accept-package-agreements') -AllowFailure
    $code = $script:LastExit
    # 0x8A15002B = no applicable upgrade, 0x8A150061 = already installed: both fine.
    if ($code -ne 0 -and $code -ne -1978335189 -and $code -ne -1978335135) {
        Write-Warn ('winget returned {0} (0x{0:X8}) for {1}.' -f $code, $Id)
    }
    Update-SessionPath
}

function Find-SystemPython {
    # A working system Python (not the Microsoft Store stub) for the pip fallback.
    $candidates = @(
        @{ Exe = 'py.exe'; Prefix = @('-3') },
        @{ Exe = 'python.exe'; Prefix = @() }
    )
    foreach ($c in $candidates) {
        if ($null -eq (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $r = Invoke-ExeCapture -FilePath $c.Exe -Arguments (@($c.Prefix) + @('--version'))
        if ($r.ExitCode -eq 0) { return $c }
    }
    return $null
}

function Find-Uv {
    $cmd = Get-Command uv.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $cmd) { return @{ Exe = $cmd.Source; Prefix = @() } }
    foreach ($p in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'))) {
        if (Test-Path -LiteralPath $p) { return @{ Exe = $p; Prefix = @() } }
    }
    return $null
}

function Install-Uv {
    $uv = Find-Uv
    if ($null -ne $uv) { return $uv }
    if ($script:HaveWinget) {
        try { Invoke-WingetInstall -Id 'astral-sh.uv' } catch { Write-Warn $_.Exception.Message }
        $uv = Find-Uv
        if ($null -ne $uv) { return $uv }
    }
    Write-Info 'Falling back to "python -m pip install --user uv".'
    $py = Find-SystemPython
    if ($null -eq $py) {
        throw 'uv could not be installed: no winget and no working Python. Install uv from https://docs.astral.sh/uv/ and re-run.'
    }
    Invoke-Exe -FilePath $py.Exe -Arguments (@($py.Prefix) + @('-m', 'pip', 'install', '--user', '--upgrade', 'uv'))
    return @{ Exe = $py.Exe; Prefix = (@($py.Prefix) + @('-m', 'uv')) }
}

function Find-Ollama {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $cmd) { return $cmd.Source }
    $p = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    return $null
}

function Get-OllamaTags([string]$BaseUrl) {
    try { return (Invoke-RestMethod -Uri ($BaseUrl + '/api/tags') -TimeoutSec 3 -UseBasicParsing) } catch { return $null }
}

function Test-OllamaHasModel($Tags, [string]$Model) {
    if ($null -eq $Tags) { return $false }
    $want = @($Model)
    if ($Model -notmatch ':') { $want += ($Model + ':latest') }
    foreach ($m in @(Get-Prop $Tags 'models' @())) {
        $n = [string](Get-Prop $m 'name' '')
        if ($want -contains $n) { return $true }
    }
    return $false
}

# ------------------------------------------------------------------------------ steps
function Invoke-Setup {
    Write-Host 'ShoutAbout setup' -ForegroundColor White
    Write-Host '================' -ForegroundColor White

    if (Test-IsAdmin) {
        Write-Host ''
        Write-Host 'This PowerShell window is running as Administrator.' -ForegroundColor Yellow
        Write-Host 'Please run setup from a NORMAL window: the hub must run as your normal user, and setup' -ForegroundColor Yellow
        Write-Host 'asks for admin rights (one UAC prompt) only for the firewall rules.' -ForegroundColor Yellow
        $ans = Read-Host 'Continue anyway? [y/N]'
        if ($ans -notmatch '^(?i)(y|yes)$') { Write-Host 'Stopped.'; $script:ExitCode = 1; return }
    }

    # ------------------------------------------------------------------ 1. prerequisites
    Write-Step 'Prerequisites'
    $Fqdn = ConvertTo-Hostname $PublicHostname   # validate early; '' when not given
    $RepoRoot =(Resolve-Path -LiteralPath (Join-Path $script:SelfDir '..')).ProviderPath.TrimEnd('\')
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml')) -or
        -not (Test-Path -LiteralPath (Join-Path $RepoRoot 'src\shoutabout'))) {
        throw ('{0} does not look like the ShoutAbout repository (pyproject.toml / src\shoutabout missing).' -f $RepoRoot)
    }
    Write-Ok ('Repository: {0}' -f $RepoRoot)

    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $ver = [Environment]::OSVersion.Version
    if ($ver.Major -lt 10 -or -not [Environment]::Is64BitOperatingSystem) {
        throw ('ShoutAbout needs 64-bit Windows 10 or 11 (found {0}).' -f $os.Caption)
    }
    Write-Ok ('{0} (build {1})' -f $os.Caption, $os.BuildNumber)

    # The hub port must not belong to another program (the hub itself is fine).
    $IntendedPort = $Port
    if (-not $script:PortGiven) {
        $p = Get-ConfigPortFromFile (Join-Path (Get-ConfigHome) 'config.toml')
        if ($null -ne $p) { $IntendedPort = [int]$p }
    }
    Assert-PortFree $IntendedPort

    if ($null -ne (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        $w = Invoke-ExeCapture -FilePath 'winget.exe' -Arguments @('--version')
        if ($w.ExitCode -eq 0) {
            $script:HaveWinget = $true
            Write-Ok ('winget {0}' -f ((@($w.StdOut) -join ' ').Trim()))
        }
    }
    if (-not $script:HaveWinget) {
        Write-Warn 'winget not found. Install "App Installer" from the Microsoft Store, or install uv/Ollama/cloudflared manually.'
    }

    $script:HaveNvidia = $false
    if ($null -ne (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
        $g = Invoke-ExeCapture -FilePath 'nvidia-smi.exe' -Arguments @('--query-gpu=name,driver_version,memory.total', '--format=csv,noheader')
        if ($g.ExitCode -eq 0 -and @($g.StdOut).Count -gt 0) {
            $script:HaveNvidia = $true
            $line = [string](@($g.StdOut)[0])
            Write-Ok ('NVIDIA GPU: {0}' -f $line.Trim())
            $fields = @($line -split ',')
            if ($fields.Count -ge 2) {
                $drv = ([string]$fields[1]).Trim()
                $drvVer = 0.0
                if ([double]::TryParse($drv, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$drvVer) -and $drvVer -lt 527.41) {
                    Write-Warn ('NVIDIA driver {0} is too old for CUDA 12 (needs 527.41+). Update it from nvidia.com or the NVIDIA app.' -f $drv)
                }
            }
        }
    }
    if (-not $script:HaveNvidia) {
        Write-Warn 'nvidia-smi not found/working: no NVIDIA driver. Whisper on CPU is slow; set [stt] device = "cpu" (or cpu_fallback = true) in config.toml.'
    }

    # ------------------------------------------------------------------ 2. uv
    Write-Step 'uv (Python package manager)'
    Update-SessionPath
    $uv = Install-Uv
    $uvVer = Invoke-ExeCapture -FilePath $uv.Exe -Arguments (@($uv.Prefix) + @('--version'))
    Write-Ok ('{0} ({1})' -f ((@($uvVer.StdOut) -join ' ').Trim()), $uv.Exe)

    # ------------------------------------------------------------------ 3. dependencies
    Write-Step 'Python 3.12 and dependencies (.venv)'
    if (@(Get-HubProcesses).Count -gt 0) { Stop-Hub }   # a running hub locks files in .venv
    Write-Info 'uv sync --locked --extra cuda --python 3.12  (first run downloads ~1 GB incl. NVIDIA cuBLAS)'
    Push-Location -LiteralPath $RepoRoot
    try {
        Invoke-Exe -FilePath $uv.Exe -Arguments (@($uv.Prefix) + @('sync', '--locked', '--extra', 'cuda', '--python', '3.12')) -AllowFailure
    } finally {
        Pop-Location
    }
    if ($script:LastExit -ne 0) {
        throw ('uv sync failed (exit {0}). If it says the lockfile needs updating, run "uv lock" in {1} (developers only), then re-run setup.' -f $script:LastExit, $RepoRoot)
    }
    $VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $VenvPythonw = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $VenvPython)) { throw ('{0} was not created by uv sync.' -f $VenvPython) }
    Write-Ok 'Dependencies installed.'

    $PythonBaseDir = ''
    $b = Invoke-ExeCapture -FilePath $VenvPython -Arguments @('-c', 'import sys; print(sys._base_executable)') -Utf8
    if ($b.ExitCode -eq 0 -and @($b.StdOut).Count -gt 0) {
        $baseExe = ([string](@($b.StdOut)[0])).Trim()
        if ($baseExe -and (Test-Path -LiteralPath $baseExe)) { $PythonBaseDir = Split-Path -Parent $baseExe }
    }
    if ($PythonBaseDir) { Write-Ok ('Base interpreter: {0}' -f $PythonBaseDir) }
    else { Write-Warn 'Could not resolve the base Python interpreter; only the port firewall rule will be created.' }

    # ------------------------------------------------------------------ 4. config
    Write-Step 'Configuration (%APPDATA%\shoutabout)'
    if ($script:PortGiven) {
        # "init --port N" writes/updates [server] port in config.toml.
        Invoke-Exe -FilePath $VenvPython -Arguments @('-m', 'shoutabout', 'init', '--port', [string]$Port) -AllowFailure
        if ($script:LastExit -eq 2) {
            # Older CLI without --port: plain init, then edit config.toml ourselves.
            Invoke-Exe -FilePath $VenvPython -Arguments @('-m', 'shoutabout', 'init')
            Set-ConfigPort -ConfigFile (Join-Path (Get-ConfigHome) 'config.toml') -NewPort $Port
        } elseif ($script:LastExit -ne 0) {
            throw ('"python -m shoutabout init --port {0}" failed (exit {1}).' -f $Port, $script:LastExit)
        }
    } else {
        Invoke-Exe -FilePath $VenvPython -Arguments @('-m', 'shoutabout', 'init')
    }
    $info = Get-HubInfo $VenvPython
    $ConfigHome = [string](Get-Prop $info 'home' (Get-ConfigHome))
    $HubPort = [int](Get-Prop $info 'port' $IntendedPort)
    if ($script:PortGiven -and $HubPort -ne $Port) {
        Write-Warn ('-Port {0} was given but config.toml says port {1}; using {1}. Edit [server] port in {2}.' -f $Port, $HubPort, (Get-Prop $info 'config_file' 'config.toml'))
    }
    if ($HubPort -ne $IntendedPort) { Assert-PortFree $HubPort }
    Write-Ok ('Config dir: {0}  (port {1})' -f $ConfigHome, $HubPort)

    # ------------------------------------------------------------------ 5. Whisper model
    Write-Step 'Whisper speech model'
    if ($SkipModel) {
        Write-Skip 'Skipped (-SkipModel). The hub downloads it on first start.'
    } else {
        Write-Info ('Downloading {0} (~1.6 GB, a few minutes on first run)...' -f (Get-Prop $info 'stt_model' 'the model'))
        Invoke-Exe -FilePath $VenvPython -Arguments @('-m', 'shoutabout', 'download-model') -AllowFailure
        if ($script:LastExit -eq 0) { Write-Ok 'Whisper model ready.' }
        else { Write-Warn ('Model download failed (exit {0}). Re-run: .\.venv\Scripts\python.exe -m shoutabout download-model' -f $script:LastExit) }
    }

    # ------------------------------------------------------------------ 6. Ollama
    Write-Step 'Ollama and the cleanup model'
    $cleanupEnabled = [bool](Get-Prop $info 'cleanup_enabled' $true)
    $cleanupModel = [string](Get-Prop $info 'cleanup_model' '')
    $ollamaUrl = ([string](Get-Prop $info 'ollama_url' 'http://127.0.0.1:11434')).TrimEnd('/')
    if ($SkipOllama) {
        Write-Skip 'Skipped (-SkipOllama).'
    } elseif (-not $cleanupEnabled) {
        Write-Skip 'Cleanup is disabled in config.toml ([cleanup] enabled = false).'
    } elseif (-not $cleanupModel) {
        Write-Warn 'No cleanup model in config; skipping Ollama.'
    } else {
        try {
            $ollama = Find-Ollama
            if ($null -eq $ollama) {
                Invoke-WingetInstall -Id 'Ollama.Ollama'
                $ollama = Find-Ollama
                if ($null -eq $ollama) { throw 'Ollama is still not found after winget install. Install it from https://ollama.com/download and re-run.' }
            }
            Write-Ok ('Ollama: {0}' -f $ollama)
            $tags = Get-OllamaTags $ollamaUrl
            if ($null -eq $tags) {
                Write-Info 'Starting Ollama...'
                $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
                if (Test-Path -LiteralPath $app) { Start-Process -FilePath $app | Out-Null }
                else { Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden | Out-Null }
                $deadline = (Get-Date).AddSeconds(60)
                while ($null -eq $tags -and (Get-Date) -lt $deadline) {
                    Start-Sleep -Seconds 2
                    $tags = Get-OllamaTags $ollamaUrl
                }
                if ($null -eq $tags) { throw ('Ollama did not answer at {0} within 60 s. Start the Ollama app and re-run.' -f $ollamaUrl) }
            }
            Write-Ok ('Ollama API answers at {0}' -f $ollamaUrl)
            if (Test-OllamaHasModel $tags $cleanupModel) {
                Write-Ok ('Model {0} already pulled.' -f $cleanupModel)
            } else {
                Write-Info ('ollama pull {0}  (~2.5 GB)' -f $cleanupModel)
                Invoke-Exe -FilePath $ollama -Arguments @('pull', $cleanupModel) -AllowFailure
                if ($script:LastExit -eq 0) { Write-Ok ('Pulled {0}.' -f $cleanupModel) }
                else { Write-Warn ('ollama pull {0} failed (exit {1}). Re-run it later; the hub pastes raw text until then.' -f $cleanupModel, $script:LastExit) }
            }
        } catch {
            Write-Warn ('Ollama: {0}' -f $_.Exception.Message)
        }
    }

    # ------------------------------------------------------------------ 7. task + firewall
    Write-Step 'Scheduled task and firewall'
    $UserId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
    $taskViaAdmin = $false
    if ($SkipTask) {
        Write-Skip 'Scheduled task skipped (-SkipTask).'
    } else {
        try {
            Register-HubTask -RepoRoot $RepoRoot -UserId $UserId
            Write-Ok ('Scheduled task "{0}" (at logon of {1}, not elevated)' -f $script:TaskName, $UserId)
        } catch {
            $msg = $_.Exception.Message
            if ($msg -match '(?i)access is denied|0x80070005|unauthorized') {
                Write-Info 'Registering the task needs admin rights here (an older copy was created elevated); doing it in the admin step.'
                $taskViaAdmin = $true
            } else {
                Write-Warn ('Could not register the scheduled task: {0}' -f $msg)
            }
        }
    }
    if ($SkipFirewall -and -not $taskViaAdmin) {
        Write-Skip 'Firewall skipped (-SkipFirewall): the iPhone cannot reach the hub over Wi-Fi without it.'
    } else {
        $resultFile = Join-Path $env:TEMP ('shoutabout-admin-{0}.json' -f [guid]::NewGuid().ToString('N'))
        $argText = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Port {1} -RepoRoot "{2}" -UserId "{3}" -ResultFile "{4}"' -f `
        (Join-Path $script:SelfDir 'setup-admin.ps1'), $HubPort, $RepoRoot, $UserId, $resultFile
        if ($PythonBaseDir) { $argText += (' -PythonDir "{0}"' -f $PythonBaseDir.TrimEnd('\')) }
        if (-not $SkipFirewall) { $argText += ' -Firewall' }
        if ($taskViaAdmin) { $argText += ' -Task' }
        Write-Info 'Opening the administrator step (approve the UAC prompt)...'
        try {
            Start-Process -FilePath (Get-PowerShellExe) -Verb RunAs -Wait -ArgumentList $argText | Out-Null
            if (Test-Path -LiteralPath $resultFile) {
                $res = [IO.File]::ReadAllText($resultFile) | ConvertFrom-Json
                Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue
                foreach ($m in @(Get-Prop $res 'messages' @())) {
                    $ms = [string]$m
                    if ($ms -match '^\[(WARN|ERROR)\]') { Write-Warn ($ms -replace '^\[\w+\]\s*', '') } else { Write-Info $ms }
                }
                if ([bool](Get-Prop $res 'ok' $false)) { Write-Ok 'Administrator step finished.' }
                else { Write-Warn 'The administrator step reported errors (see above).' }
            } else {
                Write-Warn ('The administrator step did not report back. Run it manually from an elevated PowerShell: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -Firewall -Port {1}' -f (Join-Path $script:SelfDir 'setup-admin.ps1'), $HubPort)
            }
        } catch {
            Write-Warn ('Administrator step not run ({0}). Re-run setup to try again.' -f $_.Exception.Message)
        }
    }
    foreach ($b in @(Get-PythonBlockRules)) { Write-Warn ('Firewall BLOCK rule for Python still active: {0}' -f $b) }

    # ------------------------------------------------------------------ 8. Cloudflare
    Write-Step 'Cloudflare Tunnel'
    if ($SkipCloudflare) {
        Write-Skip 'Skipped (-SkipCloudflare). Later: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1'
    } else {
        $cf = @{ Port = $HubPort }
        if ($Fqdn) { $cf['PublicHostname'] = $Fqdn }
        try {
            $global:LASTEXITCODE = 0
            & (Join-Path $script:SelfDir 'setup-cloudflare.ps1') @cf
            if ($LASTEXITCODE -ne 0) { Write-Warn ('setup-cloudflare.ps1 exited with {0}. Re-run it on its own when ready.' -f $LASTEXITCODE) }
        } catch {
            Write-Warn ('Cloudflare setup stopped: {0}' -f $_.Exception.Message)
        }
    }

    # ------------------------------------------------------------------ 9. install state
    Write-Step 'Install state'
    $statePath = Join-Path $ConfigHome 'install-state.json'
    $state = Read-InstallState $statePath
    $state['repo'] = $RepoRoot
    $state['port'] = $HubPort
    $state['task_name'] = $script:TaskName
    $state['firewall_rule_name'] = ('ShoutAbout-Hub-TCP-{0}' -f $HubPort)
    if ($Fqdn) { $state['public_hostname'] = $Fqdn }
    foreach ($k in @('public_hostname', 'tunnel_name')) { if (-not $state.Contains($k)) { $state[$k] = $null } }
    if (-not $state.Contains('tunnel_mode')) { $state['tunnel_mode'] = 'none' }
    if (-not ($state['created'] -is [System.Collections.IDictionary])) { $state['created'] = [ordered]@{} }
    foreach ($k in @('tunnel', 'dns_route', 'cloudflared_service', 'cloudflared_config')) {
        if (-not $state['created'].Contains($k)) { $state['created'][$k] = $false }
    }
    Write-InstallState -Path $statePath -State $state
    Write-Ok ('Wrote {0}' -f $statePath)
    $Fqdn = [string](Get-Prop $state 'public_hostname' '')

    # ------------------------------------------------------------------ 10. start
    Write-Step 'Start the hub'
    if ($NoStart) {
        Write-Skip 'Skipped (-NoStart).'
    } else {
        Stop-Hub
        foreach ($o in @(Get-ForeignPortListeners $HubPort)) {
            Write-Warn ('Port {0} is now used by PID {1} {2}; the hub will fail to start. Stop it or re-run setup with -Port <free port>.' -f $HubPort, $o.ProcessId, $o.Name)
        }
        $started = $false
        $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            try { Start-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction Stop; $started = $true; Write-Info ('Started task "{0}".' -f $script:TaskName) }
            catch { Write-Info ('Could not start the task ({0}); starting the hub directly.' -f $_.Exception.Message) }
        }
        if (-not $started) {
            Start-Process -FilePath $VenvPythonw -ArgumentList '-m shoutabout run' -WorkingDirectory $RepoRoot | Out-Null
            Write-Info 'Started pythonw -m shoutabout run.'
        }
        if (Wait-HubHealth -HubPort $HubPort -Seconds 30) {
            Write-Ok ('Hub answers on http://127.0.0.1:{0}/health (the model may still be loading; watch the tray icon).' -f $HubPort)
        } else {
            Write-Warn ('Hub did not answer on port {0} within 30 s. Check {1}' -f $HubPort, (Get-Prop $info 'log_dir' (Join-Path $ConfigHome 'logs')))
        }
    }

    # ------------------------------------------------------------------ 11. doctor + summary
    Write-Step 'Health check (doctor) and summary'
    $doctorArgs = @('-m', 'shoutabout', 'doctor')
    if (-not $Fqdn) { $doctorArgs += '--skip-tunnel' }
    Invoke-Exe -FilePath $VenvPython -Arguments $doctorArgs -AllowFailure
    $doctorExit = $script:LastExit
    if ($doctorExit -ne 0) { Write-Warn 'doctor reported failures (see [FAIL] lines above and their "fix:" hints).' }

    try { $info = Get-HubInfo $VenvPython } catch { }
    $lanUrl = [string](Get-Prop $info 'lan_url' '')
    $net = Get-DefaultRouteProfile

    Write-Host ''
    Write-Host 'Summary' -ForegroundColor Cyan
    Write-Host ('  Config dir : {0}' -f $ConfigHome)
    Write-Host ('  Vocab file : {0}  (edit any time, reloads automatically)' -f (Get-Prop $info 'vocab_file' (Join-Path $ConfigHome 'vocab.txt')))
    if ($lanUrl) { Write-Host ('  LAN URL    : {0}' -f $lanUrl) } else { Write-Host '  LAN URL    : (not detected; set [server] advertise_host in config.toml)' }
    if ($Fqdn) { Write-Host ('  Public URL : https://{0}' -f $Fqdn) }
    Write-Host '  Hub token  : not shown here. Put it on the iPhone with:'
    Write-Host '                 .\.venv\Scripts\python.exe -m shoutabout token --qr     (or: token --copy)'
    if ($null -ne $net) {
        $label = if ($net.Name) { $net.Name } else { $net.InterfaceAlias }
        if ($net.Category -eq 'Public') {
            Write-Host ('  Network    : "{0}" ({1}) is PUBLIC - the firewall rule only allows Private networks.' -f $label, $net.InterfaceAlias) -ForegroundColor Yellow
            Write-Host ('               Fix: Settings > Network & internet > Wi-Fi (or Ethernet) > "{0}" > Network profile type > Private' -f $label) -ForegroundColor Yellow
            $script:Warnings.Add('Network profile is Public; switch it to Private (see above).')
        } else {
            Write-Host ('  Network    : "{0}" ({1}) is {2}.' -f $label, $net.InterfaceAlias, $net.Category)
        }
    } else {
        Write-Host '  Network    : could not determine the network profile of the default route.'
    }
    if ($script:Warnings.Count -gt 0) {
        Write-Host ''
        Write-Host ('Warnings ({0}):' -f $script:Warnings.Count) -ForegroundColor Yellow
        foreach ($w in $script:Warnings) { Write-Host ('  - {0}' -f $w) -ForegroundColor Yellow }
    }
    Write-Host ''
    Write-Host 'Next steps' -ForegroundColor Cyan
    Write-Host '  - SETUP.md step 5: Cloudflare Access (application, service token, Service Auth policy),'
    Write-Host '    then put team_domain + aud into config.toml [access] and restart the hub.'
    Write-Host '  - SETUP.md step 6: the first test from the iPhone.'
    Write-Host '  - Test all routes from this PC:'
    Write-Host '      powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_test_audio.ps1'
    Write-Host '      powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1'
    Write-Host '  - Re-check any time: .\.venv\Scripts\python.exe -m shoutabout doctor'
    if ($doctorExit -ne 0) { $script:ExitCode = 1 }
}

# ------------------------------------------------------------------------------ entry point
# Dot-sourcing (". .\setup.ps1") only loads the functions (used by tests/test_scripts.py).
if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Setup
    } catch {
        Write-Host ''
        Write-Host ('Setup stopped at step {0}/{1}: {2}' -f $script:StepNo, $script:StepTotal, $_.Exception.Message) -ForegroundColor Red
        Write-Host 'Fix the problem and re-run setup (it is safe to re-run).' -ForegroundColor Red
        if ($_.Exception.Message -match '(?i)not digitally signed|cannot be loaded|execution polic') {
            Write-Host 'If the repo was downloaded as a ZIP, unblock the scripts first:  Get-ChildItem -Recurse .\scripts | Unblock-File' -ForegroundColor Yellow
        }
        $script:ExitCode = 1
    }
    exit $script:ExitCode
}
