<#
.SYNOPSIS
    Publishes the ShoutAbout hub through a Cloudflare Tunnel (and walks you through
    Cloudflare Access, which must protect it).

.DESCRIPTION
    Run from a normal (not elevated) PowerShell; admin-only steps ask for UAC themselves.
    setup.ps1 runs this script for you; run it on its own to change the tunnel later:

        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.example.com

    1. Installs cloudflared with winget if it is missing.
    2. Detects an existing "cloudflared" Windows service (its token is never printed).
    3. Prints the Cloudflare Access steps (service token, Service Auth policy, Access
       application with "401 Response for Service Auth policies") and waits: the Access
       application must exist BEFORE the hostname is routed, or the hub is reachable by anyone.
    4. Routes the hostname to http://127.0.0.1:<port>, depending on -Mode:
         existing  A cloudflared service already runs on this PC. Remotely-managed (token):
                   prints the dashboard steps to add a published-application route; nothing
                   changes locally. Locally-managed (config file): prints the ingress rule,
                   "cloudflared tunnel route dns" and restart commands (nothing is edited).
         remote    New dashboard-managed tunnel (recommended by Cloudflare): you create it in
                   the dashboard and paste its token (hidden input); the cloudflared service
                   is installed elevated. Then you add the route in the dashboard.
         local     New CLI-managed tunnel: cloudflared tunnel login / create / route dns,
                   config.yml in C:\Windows\System32\config\systemprofile\.cloudflared, and the
                   cloudflared service (elevated, one UAC prompt).
         auto      existing if a cloudflared service is found, otherwise asks (remote/local).
       remote/local refuse to run next to a cloudflared service they did not create unless you
       confirm; then a separate service named "cloudflared-shoutabout" is used.
    5. Updates install-state.json in the config dir (public_hostname, tunnel_mode,
       tunnel_name, created{...}) so doctor and uninstall.ps1 know what exists.

.PARAMETER PublicHostname
    Hostname to publish, e.g. dictate.example.com (asked for if omitted).

.PARAMETER Port
    Hub port on this PC. Default: the port from "python -m shoutabout info --json" (config.toml
    [server] port), else install-state.json, else 8765.

.PARAMETER Mode
    auto (default), existing, remote or local. See the description.

.PARAMETER TunnelName
    Tunnel name for remote/local mode. Default "shoutabout".

.PARAMETER AdminAction
    Internal: used when the script re-starts itself elevated. Do not use.

.PARAMETER CloudflaredExe
    Internal (elevated step): path of cloudflared.exe.

.PARAMETER ServiceName
    Internal (elevated step): Windows service name to install.

.PARAMETER TokenFile
    Internal (elevated step): temporary file holding the tunnel token (deleted after use).

.PARAMETER StagingDir
    Internal (elevated step): folder with cert.pem, <tunnel-id>.json and the staged config.

.PARAMETER TunnelId
    Internal (elevated step): tunnel UUID for local mode.

.PARAMETER ResultFile
    Internal (elevated step): JSON result file.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.example.com

.EXAMPLE
    # This PC already runs a dashboard-managed tunnel: just print the route + Access steps.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.example.com -Mode existing

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.example.com -Mode local -TunnelName shoutabout
#>
[CmdletBinding()]
param(
    [string]$PublicHostname = '',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [ValidateSet('auto', 'existing', 'remote', 'local')][string]$Mode = 'auto',
    [string]$TunnelName = 'shoutabout',
    [string]$AdminAction = '',
    [string]$CloudflaredExe = '',
    [string]$ServiceName = '',
    [string]$TokenFile = '',
    [string]$StagingDir = '',
    [string]$TunnelId = '',
    [string]$ResultFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue } catch { }

$script:SelfPath = $PSCommandPath
$script:PortGiven = $PSBoundParameters.ContainsKey('Port')
$script:StepNo = 0
$script:StepTotal = 6
$script:LastExit = 0
$script:ExitCode = 0
$script:Ok = $true
$script:Messages = New-Object System.Collections.Generic.List[string]
$script:FilesCreated = New-Object System.Collections.Generic.List[string]
$script:ServiceCreated = $false
$script:SharedServiceName = 'cloudflared-shoutabout'
$script:StagingConfigName = 'shoutabout-staging.yml'

# ------------------------------------------------------------------------------ output helpers
function Get-RedactedText([string]$Text) {
    # Never let a tunnel token reach the screen or a file.
    if (-not $Text) { return $Text }
    $t = [regex]::Replace($Text, '(?im)(--token(?:-file)?)(?:\s*=\s*|\s+).*$', '$1 <redacted>')
    $t = [regex]::Replace($t, '(?i)(service\s+install\s+)\S+', '$1<redacted>')
    $t = [regex]::Replace($t, 'eyJ[A-Za-z0-9_\-+/=.]{8,}', '<redacted>')
    return $t
}
function Write-Step([string]$Title) {
    $script:StepNo++
    Write-Host ''
    Write-Host ('[{0}/{1}] {2}' -f $script:StepNo, $script:StepTotal, $Title) -ForegroundColor Cyan
}
function Write-Ok([string]$Text) { Write-Host ('  OK    {0}' -f (Get-RedactedText $Text)) -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host ('  ..    {0}' -f (Get-RedactedText $Text)) }
function Write-Warn([string]$Text) { Write-Host ('  WARN  {0}' -f (Get-RedactedText $Text)) -ForegroundColor Yellow }
function Write-Text([string]$Text) { Write-Host ('        {0}' -f $Text) }
function Add-Message {
    # Elevated step: print and remember for the result file.
    param([ValidateSet('OK', 'INFO', 'WARN', 'ERROR')][string]$Level, [string]$Text)
    $clean = Get-RedactedText $Text
    $script:Messages.Add(('[{0}] {1}' -f $Level, $clean))
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERROR' { 'Red' } default { 'Gray' } }
    Write-Host ('  {0,-5} {1}' -f $Level, $clean) -ForegroundColor $color
    if ($Level -eq 'ERROR') { $script:Ok = $false }
}

# ------------------------------------------------------------------------------ generic helpers
function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Exe {
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$Arguments = @(), [switch]$AllowFailure)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & $FilePath @Arguments | Out-Host
    $script:LastExit = $LASTEXITCODE
    if ($script:LastExit -ne 0 -and -not $AllowFailure) {
        throw ('"{0} {1}" failed with exit code {2}.' -f (Split-Path -Leaf $FilePath), (Get-RedactedText ($Arguments -join ' ')), $script:LastExit)
    }
}

function Invoke-ExeCapture {
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$Arguments = @())
    $ErrorActionPreference = 'Continue'
    $out = New-Object System.Collections.Generic.List[string]
    $err = New-Object System.Collections.Generic.List[string]
    $code = -1
    try {
        $global:LASTEXITCODE = 0
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $err.Add($_.ToString()) } else { $out.Add([string]$_) }
        }
        $code = $LASTEXITCODE
    } catch {
        $err.Add($_.Exception.Message)
    }
    return [pscustomobject]@{ ExitCode = $code; StdOut = $out.ToArray(); StdErr = $err.ToArray() }
}

function Update-SessionPath {
    $parts = New-Object System.Collections.Generic.List[string]
    $sources = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
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

function Resolve-HubPort($State) {
    # -Port if given, else "python -m shoutabout info --json" port, else install-state, else 8765.
    if ($script:PortGiven) { return $Port }
    $py = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $py) {
        $r = Invoke-ExeCapture -FilePath $py -Arguments @('-m', 'shoutabout', 'info', '--json')
        if ($r.ExitCode -eq 0) {
            try {
                $info = (@($r.StdOut) -join "`n") | ConvertFrom-Json
                $p = [int](Get-Prop $info 'port' 0)
                if ($p -gt 0) { return $p }
            } catch { }
        }
    }
    $p = [int](Get-Prop $State 'port' 0)
    if ($p -gt 0) { return $p }
    return $Port
}

# ------------------------------------------------------------------------------ cloudflared helpers
function Get-SystemCloudflaredDir {
    # Home of the LocalSystem account, where the cloudflared service looks for its files.
    return (Join-Path $env:windir 'System32\config\systemprofile\.cloudflared')
}

function Get-LocalConfigName([string]$Name) {
    if ($Name -eq 'cloudflared') { return 'config.yml' }
    return ('{0}.yml' -f $Name)
}

function Get-TunnelKind([string]$ImagePath) {
    # remote = token based (dashboard-managed); local = config file (CLI-managed).
    if ($ImagePath -match '(?i)--token(-file)?(\s|=|$)') { return @{ Kind = 'remote'; ConfigPath = '' } }
    if ($ImagePath -match '(?i)--config(?:\s*=\s*|\s+)(?:"([^"]+)"|(\S+))') {
        $p = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
        return @{ Kind = 'local'; ConfigPath = $p }
    }
    return @{ Kind = 'local'; ConfigPath = (Join-Path (Get-SystemCloudflaredDir) 'config.yml') }
}

function Get-TunnelServiceInfo([string]$Name) {
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $svc) { return $null }
    $img = ''
    try {
        $img = [string](Get-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Services\{0}' -f $svc.Name) -Name ImagePath -ErrorAction Stop).ImagePath
    } catch { $img = '' }
    $kind = Get-TunnelKind $img
    $info = [pscustomobject]@{
        Name       = [string]$svc.Name
        Status     = [string]$svc.Status
        Redacted   = (Get-RedactedText $img)
        Kind       = $kind.Kind
        ConfigPath = $kind.ConfigPath
    }
    $img = $null
    return $info
}

function Find-Cloudflared {
    $cmd = Get-Command cloudflared.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'cloudflared\cloudflared.exe'),
        (Join-Path $env:ProgramFiles 'cloudflared\cloudflared.exe'),
        (Join-Path $env:SystemDrive 'Cloudflared\bin\cloudflared.exe')
    )
    foreach ($p in $candidates) { if (Test-Path -LiteralPath $p) { return $p } }
    return $null
}

function Get-CloudflaredVersion([string]$Exe) {
    $r = Invoke-ExeCapture -FilePath $Exe -Arguments @('--version')
    $text = (@($r.StdOut) + @($r.StdErr)) -join ' '
    if ($text -match '(\d+)\.(\d+)\.(\d+)') { return [version]('{0}.{1}.{2}' -f $Matches[1], $Matches[2], $Matches[3]) }
    return $null
}

function Get-TunnelByName([string]$Exe, [string]$Name) {
    $r = Invoke-ExeCapture -FilePath $Exe -Arguments @('tunnel', 'list', '--output', 'json')
    if ($r.ExitCode -ne 0) {
        throw ('"cloudflared tunnel list" failed: {0}' -f ((@($r.StdErr) | Select-Object -Last 2) -join ' '))
    }
    $text = (@($r.StdOut) -join "`n").Trim()
    if (-not $text -or $text -eq 'null') { return $null }
    $parsed = $text | ConvertFrom-Json
    foreach ($t in $parsed) {
        if ([string](Get-Prop $t 'name' '') -eq $Name) { return $t }
    }
    return $null
}

function New-LocalConfigYaml([string]$Id, [string]$CredentialsFile, [string]$Fqdn, [int]$HubPort) {
    $lines = @(
        '# Written by ShoutAbout scripts\setup-cloudflare.ps1 (locally-managed tunnel).',
        ('tunnel: {0}' -f $Id),
        ('credentials-file: {0}' -f $CredentialsFile),
        'ingress:',
        ('  - hostname: {0}' -f $Fqdn),
        ('    service: http://127.0.0.1:{0}' -f $HubPort),
        '  - service: http_status:404'
    )
    return (($lines -join "`r`n") + "`r`n")
}

function Split-Fqdn([string]$Fqdn) {
    $labels = @($Fqdn.Split('.'))
    if ($labels.Count -le 2) { return @{ Sub = '(leave empty)'; Zone = $Fqdn } }
    return @{ Sub = $labels[0]; Zone = (($labels[1..($labels.Count - 1)]) -join '.') }
}

function Wait-ServiceStatus([string]$Name, [string]$Status, [int]$Seconds = 20) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -ne $s -and [string]$s.Status -eq $Status) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Restart-TunnelService([string]$Name) {
    $s = Get-Service -Name $Name -ErrorAction Stop
    if ([string]$s.Status -ne 'Stopped') {
        & sc.exe stop $Name | Out-Null
        [void](Wait-ServiceStatus -Name $Name -Status 'Stopped' -Seconds 20)
    }
    & sc.exe start $Name | Out-Null
    if (Wait-ServiceStatus -Name $Name -Status 'Running' -Seconds 20) {
        Add-Message OK ('Service {0} is running.' -f $Name)
    } else {
        Add-Message ERROR ('Service {0} did not start. Check Event Viewer > Windows Logs > Application (source cloudflared).' -f $Name)
    }
}

function Invoke-SelfElevated([hashtable]$Params) {
    $result = Join-Path $env:TEMP ('shoutabout-cf-{0}.json' -f [guid]::NewGuid().ToString('N'))
    $argText = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script:SelfPath
    foreach ($k in $Params.Keys) { $argText += (' -{0} "{1}"' -f $k, ([string]$Params[$k]).TrimEnd('\')) }
    $argText += (' -ResultFile "{0}"' -f $result)
    Write-Info 'Opening the administrator step (approve the UAC prompt)...'
    try {
        Start-Process -FilePath (Get-PowerShellExe) -Verb RunAs -Wait -ArgumentList $argText | Out-Null
    } catch {
        throw ('The administrator step was cancelled or could not start: {0}' -f $_.Exception.Message)
    }
    if (-not (Test-Path -LiteralPath $result)) { throw 'The administrator step did not report a result.' }
    $res = [IO.File]::ReadAllText($result) | ConvertFrom-Json
    Remove-Item -LiteralPath $result -Force -ErrorAction SilentlyContinue
    foreach ($m in @(Get-Prop $res 'messages' @())) { Write-Info ([string]$m) }
    if (-not [bool](Get-Prop $res 'ok' $false)) { throw 'The administrator step reported errors (see above).' }
    return $res
}

# ------------------------------------------------------------------------------ instructions
function Show-AccessSteps([string]$Fqdn, [string]$ConfigFile) {
    $parts = Split-Fqdn $Fqdn
    Write-Host '  Protect the hostname with Cloudflare Access FIRST. Without an Access application a' -ForegroundColor Yellow
    Write-Host '  published route is reachable by anyone on the Internet. (SETUP.md step 5b)' -ForegroundColor Yellow
    Write-Text ''
    Write-Text 'a) Service token: Zero Trust > Access controls > Service credentials > Service Tokens >'
    Write-Text '   Create Service Token. Name it (e.g. shoutabout-iphone), pick a duration, Generate token.'
    Write-Text '   Copy the Client ID and Client Secret now - the secret is shown only once.'
    Write-Text 'b) Policy: Zero Trust > Access controls > Policies > Add a policy.'
    Write-Text '   Action: Service Auth. Include: Service Token = the token from a). Save.'
    Write-Text 'c) Application: Zero Trust > Access controls > Applications > Create new application >'
    Write-Text ('   Self-hosted and private > Add public hostname: subdomain "{0}", domain "{1}".' -f $parts.Sub, $parts.Zone)
    Write-Text '   Access policies: select the policy from b).'
    Write-Text '   Additional settings: turn ON "401 Response for Service Auth policies". Create.'
    Write-Text 'd) Copy the Application Audience (AUD) Tag (Applications > Configure > Additional settings)'
    Write-Text '   and your team domain (Zero Trust > Settings). Put them into'
    Write-Text ('   {0}:' -f $ConfigFile)
    Write-Text '       [access]'
    Write-Text '       team_domain = "<your-team>.cloudflareaccess.com"'
    Write-Text '       aud = ["<AUD tag>"]'
    Write-Text '   then restart the hub (tray icon > Quit, then: Start-ScheduledTask -TaskName "ShoutAbout Hub").'
}

function Show-RouteSteps([string]$Fqdn, [int]$HubPort, [string]$TunnelLabel) {
    $parts = Split-Fqdn $Fqdn
    Write-Text ('Cloudflare dashboard > Networking > Tunnels > {0} > Routes tab > Add route >' -f $TunnelLabel)
    Write-Text 'Published application:'
    Write-Text ('    Subdomain   : {0}' -f $parts.Sub)
    Write-Text ('    Domain      : {0}' -f $parts.Zone)
    Write-Text '    Path        : (leave empty)'
    Write-Text ('    Service URL : http://127.0.0.1:{0}' -f $HubPort)
    Write-Text 'then Add route. (Zero Trust dashboard: Networks > Connectors > your tunnel > Edit >'
    Write-Text ('Published application routes > Add; Service type HTTP, URL 127.0.0.1:{0}.)' -f $HubPort)
}

function Show-AccessReminder([string]$Fqdn, [string]$ConfigFile) {
    Write-Host ''
    Write-Host 'Reminder - Cloudflare Access' -ForegroundColor Cyan
    Write-Text ('* The Access application for {0} must exist before the route goes live.' -f $Fqdn)
    Write-Text '* Service Token + a Service Auth policy on that application.'
    Write-Text '* Application > Additional settings: "401 Response for Service Auth policies" ON.'
    Write-Text ('* team_domain + aud in {0} [access], then restart the hub.' -f $ConfigFile)
    Write-Text '* The iPhone Shortcut sends CF-Access-Client-Id / CF-Access-Client-Secret plus the hub token.'
    Write-Text '* Check it: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1'
}

function Wait-AccessReady {
    $a = Read-Host '  Press Enter once the Access application exists (or type "later" to continue without it)'
    if ($a -match '^(?i)later$') {
        Write-Warn 'Continuing WITHOUT Access. Create it before relying on the public route.'
        return $false
    }
    return $true
}

# ------------------------------------------------------------------------------ elevated actions
function Invoke-AdminInstallRemote {
    if (-not $CloudflaredExe -or -not (Test-Path -LiteralPath $CloudflaredExe)) { throw 'cloudflared.exe not found.' }
    if (-not $TokenFile -or -not (Test-Path -LiteralPath $TokenFile)) { throw 'Token file missing.' }
    $token = ([IO.File]::ReadAllText($TokenFile)).Trim()
    Remove-Item -LiteralPath $TokenFile -Force -ErrorAction SilentlyContinue
    if (-not $token) { throw 'The tunnel token was empty.' }
    if ($null -ne (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        $token = $null
        throw ('A service named {0} already exists; not touching it.' -f $ServiceName)
    }
    if ($ServiceName -eq 'cloudflared') {
        # Official way: cloudflared.exe service install <TOKEN>
        $r = Invoke-ExeCapture -FilePath $CloudflaredExe -Arguments @('service', 'install', $token)
        $token = $null
        foreach ($l in (@($r.StdOut) + @($r.StdErr))) { if ($l) { Write-Host ('        {0}' -f (Get-RedactedText $l)) } }
        if ($r.ExitCode -ne 0) { throw ('cloudflared service install failed (exit {0}).' -f $r.ExitCode) }
        $script:ServiceCreated = $true
        Add-Message OK 'Installed the cloudflared service (remotely-managed tunnel).'
    } else {
        # Second service next to an existing one: token in a protected file (--token-file).
        $sysDir = Get-SystemCloudflaredDir
        New-Item -ItemType Directory -Force -Path $sysDir | Out-Null
        $tf = Join-Path $sysDir ('{0}.token' -f $ServiceName)
        [IO.File]::WriteAllText($tf, $token, (New-Object System.Text.UTF8Encoding $false))
        $token = $null
        & icacls.exe $tf /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
        $script:FilesCreated.Add($tf)
        $bin = '"{0}" tunnel run --token-file "{1}"' -f $CloudflaredExe, $tf
        New-Service -Name $ServiceName -BinaryPathName $bin -DisplayName 'Cloudflared agent (ShoutAbout)' -StartupType Automatic | Out-Null
        & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
        $script:ServiceCreated = $true
        Add-Message OK ('Created service {0} (token file {1}).' -f $ServiceName, $tf)
    }
    Restart-TunnelService -Name $ServiceName
}

function Invoke-AdminInstallLocal {
    if (-not $CloudflaredExe -or -not (Test-Path -LiteralPath $CloudflaredExe)) { throw 'cloudflared.exe not found.' }
    if (-not $TunnelId) { throw 'Missing -TunnelId.' }
    $sysDir = Get-SystemCloudflaredDir
    New-Item -ItemType Directory -Force -Path $sysDir | Out-Null
    $cfgPath = Join-Path $sysDir (Get-LocalConfigName $ServiceName)
    $copies = @(
        @{ Src = (Join-Path $StagingDir 'cert.pem'); Dst = (Join-Path $sysDir 'cert.pem'); Optional = $true },
        @{ Src = (Join-Path $StagingDir ('{0}.json' -f $TunnelId)); Dst = (Join-Path $sysDir ('{0}.json' -f $TunnelId)); Optional = $false },
        @{ Src = (Join-Path $StagingDir $script:StagingConfigName); Dst = $cfgPath; Optional = $false }
    )
    foreach ($c in $copies) {
        if (-not (Test-Path -LiteralPath $c.Src)) {
            if ($c.Optional) { continue }
            throw ('Missing {0}' -f $c.Src)
        }
        $existed = Test-Path -LiteralPath $c.Dst
        Copy-Item -LiteralPath $c.Src -Destination $c.Dst -Force
        if (-not $existed) { $script:FilesCreated.Add($c.Dst) }
        Add-Message OK ('Copied {0}' -f $c.Dst)
    }
    $image = '"{0}" --config "{1}" tunnel run' -f $CloudflaredExe, $cfgPath
    if ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        if ($ServiceName -eq 'cloudflared') {
            $r = Invoke-ExeCapture -FilePath $CloudflaredExe -Arguments @('service', 'install')
            foreach ($l in (@($r.StdOut) + @($r.StdErr))) { if ($l) { Write-Host ('        {0}' -f (Get-RedactedText $l)) } }
            if ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
                throw ('cloudflared service install failed (exit {0}).' -f $r.ExitCode)
            }
        } else {
            New-Service -Name $ServiceName -BinaryPathName $image -DisplayName 'Cloudflared agent (ShoutAbout)' -StartupType Automatic | Out-Null
            & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
        }
        $script:ServiceCreated = $true
        Add-Message OK ('Installed service {0}.' -f $ServiceName)
    } else {
        Add-Message INFO ('Service {0} already exists (created by an earlier run); updating it.' -f $ServiceName)
    }
    # Official Windows instructions: point the service at the config file via ImagePath.
    Set-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Services\{0}' -f $ServiceName) -Name ImagePath -Value $image -Type ExpandString
    Add-Message OK ('Service command: {0}' -f $image)
    Restart-TunnelService -Name $ServiceName
}

function Save-AdminResult {
    if (-not $ResultFile) { return }
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    $result = [ordered]@{
        ok              = [bool]$script:Ok
        messages        = $script:Messages.ToArray()
        files_created   = $script:FilesCreated.ToArray()
        service_created = [bool]$script:ServiceCreated
        service_name    = $ServiceName
        service_status  = $(if ($null -ne $svc) { [string]$svc.Status } else { $null })
    }
    [IO.File]::WriteAllText($ResultFile, (ConvertTo-Json -InputObject $result -Depth 5), (New-Object System.Text.UTF8Encoding $false))
}

function Invoke-AdminEntry {
    Write-Host ''
    Write-Host ('ShoutAbout Cloudflare setup (administrator step: {0})' -f $AdminAction) -ForegroundColor Cyan
    try {
        if (-not (Test-IsAdmin)) { throw 'This step must run as Administrator.' }
        if (-not $ServiceName) { throw 'Missing -ServiceName.' }
        switch ($AdminAction) {
            'InstallRemote' { Invoke-AdminInstallRemote }
            'InstallLocal' { Invoke-AdminInstallLocal }
            default { throw ('Unknown -AdminAction {0}' -f $AdminAction) }
        }
    } catch {
        Add-Message ERROR $_.Exception.Message
    } finally {
        if ($TokenFile -and (Test-Path -LiteralPath $TokenFile)) { Remove-Item -LiteralPath $TokenFile -Force -ErrorAction SilentlyContinue }
    }
    Save-AdminResult
    if (-not $script:Ok) {
        [void](Read-Host 'Press Enter to close')
        return 1
    }
    return 0
}

# ------------------------------------------------------------------------------ main flow
function Read-TunnelToken {
    for ($i = 0; $i -lt 3; $i++) {
        $sec = Read-Host '  Paste the tunnel token or the whole "cloudflared.exe service install ..." command (hidden)' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $words = @(([string]$plain).Trim() -split '\s+' | Where-Object { $_ })
        $plain = $null
        if ($words.Count -gt 0) {
            $candidate = ([string]$words[$words.Count - 1]).Trim('"', "'")
            if ($candidate -match '^[A-Za-z0-9_\-+/=.]{40,}$') { return $candidate }
        }
        Write-Warn 'That does not look like a tunnel token (a long string, usually starting with "eyJ"). Try again.'
    }
    throw 'No valid tunnel token entered.'
}

function Invoke-CloudflareSetup {
    Write-Host 'ShoutAbout - Cloudflare Tunnel' -ForegroundColor White
    $configHome = Get-ConfigHome
    $configFile = Join-Path $configHome 'config.toml'
    $statePath = Join-Path $configHome 'install-state.json'
    $state = Read-InstallState $statePath
    $prevCreated = Get-Prop $state 'created' $null
    $prevMode = [string](Get-Prop $state 'tunnel_mode' 'none')
    $prevSvcName = [string](Get-Prop $state 'cloudflared_service_name' 'cloudflared')
    $prevTunnelName = [string](Get-Prop $state 'tunnel_name' '')
    $hubPort = Resolve-HubPort $state

    # ------------------------------------------------------------------ 1. hostname
    Write-Step 'Public hostname'
    $Fqdn = ConvertTo-Hostname $PublicHostname
    while (-not $Fqdn) {
        $prev = [string](Get-Prop $state 'public_hostname' '')
        $prompt = '  Public hostname for the hub (e.g. dictate.example.com)'
        if ($prev) { $prompt += (' [Enter = {0}]' -f $prev) } else { $prompt += ' [Enter = skip for now]' }
        $ans = Read-Host $prompt
        if (-not $ans -and $prev) { $ans = $prev }
        if (-not $ans) {
            Write-Warn 'No hostname given: skipping the Cloudflare step. Later: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname <host>'
            return
        }
        try { $Fqdn = ConvertTo-Hostname $ans } catch { Write-Warn $_.Exception.Message }
    }
    Write-Ok ('https://{0}  ->  http://127.0.0.1:{1}' -f $Fqdn, $hubPort)

    # ------------------------------------------------------------------ 2. cloudflared
    Write-Step 'cloudflared'
    $exe = Find-Cloudflared
    if ($null -eq $exe) {
        if ($null -eq (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            throw 'cloudflared is not installed and winget is missing. Install cloudflared (https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) and re-run.'
        }
        Write-Info 'winget install --id Cloudflare.cloudflared -e'
        Invoke-Exe -FilePath 'winget.exe' -Arguments @('install', '--id', 'Cloudflare.cloudflared', '-e', '--accept-source-agreements', '--accept-package-agreements') -AllowFailure
        Update-SessionPath
        $exe = Find-Cloudflared
        if ($null -eq $exe) { throw 'cloudflared is still not found after winget install; open a new PowerShell window and re-run.' }
    }
    $cfVersion = Get-CloudflaredVersion $exe
    Write-Ok ('{0} (version {1})' -f $exe, $(if ($null -ne $cfVersion) { $cfVersion } else { 'unknown' }))

    # ------------------------------------------------------------------ 3. existing service
    Write-Step 'Existing tunnel service on this PC'
    $oursSvc = $null
    if (($prevMode -eq 'remote' -or $prevMode -eq 'local') -and [bool](Get-Prop $prevCreated 'cloudflared_service' $false)) {
        $oursSvc = Get-TunnelServiceInfo $prevSvcName
    }
    $mainSvc = Get-TunnelServiceInfo 'cloudflared'
    $foreignSvc = $null
    if ($null -ne $mainSvc -and ($null -eq $oursSvc -or $oursSvc.Name -ne $mainSvc.Name)) { $foreignSvc = $mainSvc }
    foreach ($s in @($mainSvc, $oursSvc)) {
        if ($null -eq $s) { continue }
        if ($null -ne $mainSvc -and $null -ne $oursSvc -and $s -eq $oursSvc -and $oursSvc.Name -eq $mainSvc.Name) { continue }
        $owner = if ($null -ne $oursSvc -and $s.Name -eq $oursSvc.Name) { 'created by ShoutAbout' } else { 'not created by ShoutAbout' }
        $kindText = if ($s.Kind -eq 'remote') { 'remotely-managed (token)' } else { ('locally-managed (config {0})' -f $s.ConfigPath) }
        Write-Ok ('Service "{0}" ({1}, {2}): {3}' -f $s.Name, $s.Status, $owner, $kindText)
        Write-Text ('ImagePath: {0}' -f $s.Redacted)
    }
    if ($null -eq $mainSvc -and $null -eq $oursSvc) { Write-Info 'No cloudflared service installed.' }

    $effMode = $Mode
    if ($Mode -eq 'auto') {
        if ($null -ne $oursSvc) {
            $effMode = $prevMode
            Write-Info ('ShoutAbout set up a {0} tunnel here earlier; re-checking it.' -f $prevMode)
        } elseif ($null -ne $foreignSvc) {
            $effMode = 'existing'
        } else {
            Write-Host ''
            Write-Text 'How should the tunnel be created?'
            Write-Text '  [R] Remotely-managed (recommended by Cloudflare): create it in the dashboard, paste its token here.'
            Write-Text '  [L] Locally-managed: cloudflared tunnel login / create / route dns + config.yml, done by this script.'
            Write-Text '  [Q] Quit'
            $c = Read-Host '  Choice [R/l/q]'
            if ($c -match '^(?i)q') { Write-Host 'Stopped.'; return 1 }
            $effMode = if ($c -match '^(?i)l') { 'local' } else { 'remote' }
        }
    }
    Write-Ok ('Mode: {0}' -f $effMode)

    $svcName = 'cloudflared'
    if ($effMode -eq 'remote' -or $effMode -eq 'local') {
        if ($null -ne $oursSvc -and $prevMode -eq $effMode) {
            $svcName = $oursSvc.Name
        } elseif ($null -ne $foreignSvc -or $null -ne $oursSvc) {
            Write-Warn 'A cloudflared service already runs on this PC. A second tunnel service would collide with it.'
            Write-Text 'Recommended: add a route to that tunnel instead:  -Mode existing'
            $c = Read-Host ('  Type YES to install a separate service named "{0}" anyway' -f $script:SharedServiceName)
            if ($c -cne 'YES') { Write-Host 'Stopped. Re-run with -Mode existing.'; return 1 }
            $svcName = $script:SharedServiceName
            if ($null -ne (Get-Service -Name $svcName -ErrorAction SilentlyContinue)) {
                throw ('Service {0} already exists. Remove it first (scripts\uninstall.ps1 -RemoveTunnel, or sc.exe delete {0}).' -f $svcName)
            }
        }
    } elseif ($effMode -eq 'existing' -and $null -eq $mainSvc -and $null -eq $oursSvc) {
        throw 'No cloudflared service found on this PC; use -Mode remote or -Mode local.'
    }

    # ------------------------------------------------------------------ 4. Access first
    Write-Step 'Cloudflare Access (do this first)'
    Show-AccessSteps -Fqdn $Fqdn -ConfigFile $configFile
    Write-Host ''
    [void](Wait-AccessReady)

    # ------------------------------------------------------------------ 5. route
    Write-Step ('Tunnel route ({0})' -f $effMode)
    $recMode = $effMode
    $recName = $TunnelName
    $recSvc = $svcName
    $recTunnelId = [string](Get-Prop $state 'tunnel_id' '')
    $created = [ordered]@{ tunnel = $false; dns_route = $false; cloudflared_service = $false; cloudflared_config = $false }
    $files = New-Object System.Collections.Generic.List[string]
    foreach ($f in @(Get-Prop $state 'cloudflared_files' @())) { if ($f) { $files.Add([string]$f) } }

    if ($effMode -eq 'existing') {
        $svc = if ($null -ne $mainSvc) { $mainSvc } else { $oursSvc }
        $isOurs = ($null -ne $oursSvc -and $svc.Name -eq $oursSvc.Name)
        if ($isOurs) {
            $recMode = $prevMode; $recName = $prevTunnelName
            foreach ($k in @($created.Keys)) { $created[$k] = [bool](Get-Prop $prevCreated $k $false) }
        } else {
            $recMode = if ($svc.Kind -eq 'remote') { 'existing-remote' } else { 'existing-local' }
            $recName = $null
            $files.Clear()
        }
        $recSvc = $svc.Name
        if ($svc.Kind -eq 'remote') {
            Write-Info 'The tunnel is managed in the Cloudflare dashboard; nothing changes on this PC.'
            Write-Text 'After the Access application exists, publish the hostname:'
            Show-RouteSteps -Fqdn $Fqdn -HubPort $hubPort -TunnelLabel '<the tunnel running on this PC>'
        } else {
            $tid = '<tunnel-name-or-UUID>'
            try {
                $m = Select-String -LiteralPath $svc.ConfigPath -Pattern '^\s*tunnel:\s*(\S+)' -ErrorAction Stop | Select-Object -First 1
                if ($null -ne $m) { $tid = $m.Matches[0].Groups[1].Value }
            } catch { }
            Write-Info ('This PC runs a locally-managed tunnel ({0}). Nothing is edited automatically.' -f $svc.ConfigPath)
            Write-Text ('1. As Administrator, edit {0} and add ABOVE the final "- service: http_status:404":' -f $svc.ConfigPath)
            Write-Text ('       - hostname: {0}' -f $Fqdn)
            Write-Text ('         service: http://127.0.0.1:{0}' -f $hubPort)
            Write-Text ('2. cloudflared tunnel route dns {0} {1}' -f $tid, $Fqdn)
            Write-Text ('3. cloudflared tunnel --config "{0}" ingress validate' -f $svc.ConfigPath)
            Write-Text ('4. As Administrator: Restart-Service {0}' -f $svc.Name)
        }
    } elseif ($effMode -eq 'remote') {
        if ($null -ne $oursSvc -and $prevMode -eq 'remote') {
            Write-Ok ('Service {0} (installed earlier by ShoutAbout) is {1}.' -f $oursSvc.Name, $oursSvc.Status)
            if ($oursSvc.Status -ne 'Running') { Write-Warn ('Start it as Administrator: Start-Service {0}' -f $oursSvc.Name) }
            foreach ($k in @($created.Keys)) { $created[$k] = [bool](Get-Prop $prevCreated $k $false) }
            if ($prevTunnelName) { $recName = $prevTunnelName }
        } else {
            if ($svcName -ne 'cloudflared' -and ($null -eq $cfVersion -or $cfVersion -lt [version]'2025.4.0')) {
                throw 'A second service needs cloudflared 2025.4.0+ (--token-file). Update: winget upgrade --id Cloudflare.cloudflared'
            }
            Write-Text '1. Cloudflare dashboard > Networking > Tunnels > Create Tunnel > Cloudflared.'
            Write-Text '   (Zero Trust dashboard: Networks > Connectors > Create a tunnel > Cloudflared.)'
            Write-Text ('2. Name it "{0}" and save.' -f $TunnelName)
            Write-Text '3. Choose Windows. Copy the command shown under "service install" - do NOT run it.'
            Write-Text '   Paste it below (input is hidden; the token never appears on screen).'
            $token = Read-TunnelToken
            $tmp = Join-Path $env:TEMP ('shoutabout-tok-{0}.tmp' -f [guid]::NewGuid().ToString('N'))
            try {
                [IO.File]::WriteAllText($tmp, $token, (New-Object System.Text.UTF8Encoding $false))
                $token = $null
                $res = Invoke-SelfElevated @{ AdminAction = 'InstallRemote'; CloudflaredExe = $exe; ServiceName = $svcName; TokenFile = $tmp }
            } finally {
                if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
            }
            foreach ($f in @(Get-Prop $res 'files_created' @())) { if ($f -and -not $files.Contains([string]$f)) { $files.Add([string]$f) } }
            $created['cloudflared_service'] = [bool](Get-Prop $res 'service_created' $false)
            Write-Ok ('Tunnel connector installed as service "{0}".' -f $svcName)
        }
        Write-Text ''
        Write-Text 'Now (Access application first!) publish the hostname in the tunnel:'
        Show-RouteSteps -Fqdn $Fqdn -HubPort $hubPort -TunnelLabel $recName
    } else {
        # ---------------------------------------------------------- local (CLI-managed)
        $cfDir = Join-Path $env:USERPROFILE '.cloudflared'
        $cert = Join-Path $cfDir 'cert.pem'
        if (-not (Test-Path -LiteralPath $cert)) {
            Write-Info ('cloudflared tunnel login: a browser opens - log in and pick the zone (domain) of {0}.' -f $Fqdn)
            Invoke-Exe -FilePath $exe -Arguments @('tunnel', 'login')
            if (-not (Test-Path -LiteralPath $cert)) { throw ('{0} was not created; login did not finish.' -f $cert) }
        }
        Write-Ok ('Cloudflare login: {0}' -f $cert)

        $createdTunnel = ($prevMode -eq 'local' -and $prevTunnelName -eq $TunnelName -and [bool](Get-Prop $prevCreated 'tunnel' $false))
        $t = Get-TunnelByName -Exe $exe -Name $TunnelName
        if ($null -eq $t) {
            Invoke-Exe -FilePath $exe -Arguments @('tunnel', 'create', $TunnelName)
            $t = Get-TunnelByName -Exe $exe -Name $TunnelName
            if ($null -eq $t) { throw ('Tunnel {0} was not found after "cloudflared tunnel create".' -f $TunnelName) }
            $createdTunnel = $true
            Write-Ok ('Created tunnel {0}' -f $TunnelName)
        } else {
            Write-Ok ('Tunnel {0} already exists.' -f $TunnelName)
        }
        $tid = [string](Get-Prop $t 'id' '')
        if ($tid -notmatch '^[0-9a-fA-F-]{36}$') { throw ('Unexpected tunnel id "{0}".' -f $tid) }
        $recTunnelId = $tid
        $cred = Join-Path $cfDir ('{0}.json' -f $tid)
        if (-not (Test-Path -LiteralPath $cred)) {
            Write-Info 'Credentials file missing on this PC; fetching it (cloudflared tunnel token --cred-file).'
            Invoke-Exe -FilePath $exe -Arguments @('tunnel', 'token', '--cred-file', $cred, $tid)
        }

        $sysDir = Get-SystemCloudflaredDir
        $staging = Join-Path $cfDir $script:StagingConfigName
        $yaml = New-LocalConfigYaml -Id $tid -CredentialsFile (Join-Path $sysDir ('{0}.json' -f $tid)) -Fqdn $Fqdn -HubPort $hubPort
        [IO.File]::WriteAllText($staging, $yaml, (New-Object System.Text.UTF8Encoding $false))
        $v = Invoke-ExeCapture -FilePath $exe -Arguments @('tunnel', '--config', $staging, 'ingress', 'validate')
        if ($v.ExitCode -eq 0) { Write-Ok 'Ingress rules valid.' }
        else { Write-Warn ('cloudflared ingress validate: {0}' -f ((@($v.StdErr) + @($v.StdOut) | Select-Object -Last 3) -join ' ')) }

        $dnsOk = ($createdTunnel -and [bool](Get-Prop $prevCreated 'dns_route' $false))
        $d = Invoke-ExeCapture -FilePath $exe -Arguments @('tunnel', 'route', 'dns', $tid, $Fqdn)
        foreach ($l in (@($d.StdOut) + @($d.StdErr))) { if ($l) { Write-Text (Get-RedactedText $l) } }
        if ($d.ExitCode -eq 0) {
            $dnsOk = $true
            Write-Ok ('DNS: {0} -> tunnel {1}' -f $Fqdn, $TunnelName)
        } else {
            Write-Warn ('"cloudflared tunnel route dns" failed. If a DNS record for {0} already exists, delete it (DNS > Records) or point it to {1}.cfargotunnel.com, then re-run.' -f $Fqdn, $tid)
        }

        $res = Invoke-SelfElevated @{ AdminAction = 'InstallLocal'; CloudflaredExe = $exe; ServiceName = $svcName; StagingDir = $cfDir; TunnelId = $tid }
        foreach ($f in @(Get-Prop $res 'files_created' @())) { if ($f -and -not $files.Contains([string]$f)) { $files.Add([string]$f) } }
        $created['tunnel'] = $createdTunnel
        $created['dns_route'] = $dnsOk
        $created['cloudflared_service'] = ([bool](Get-Prop $res 'service_created' $false)) -or ($null -ne $oursSvc -and $oursSvc.Name -eq $svcName)
        $created['cloudflared_config'] = $true
    }

    # ------------------------------------------------------------------ 6. state
    Write-Step 'Install state'
    $state = Read-InstallState $statePath    # re-read: setup.ps1 may have written meanwhile
    $state['public_hostname'] = $Fqdn
    $state['tunnel_mode'] = $recMode
    $state['tunnel_name'] = $recName
    $state['created'] = $created
    $state['cloudflared_service_name'] = $recSvc
    $state['tunnel_id'] = $(if ($recTunnelId) { $recTunnelId } else { $null })
    $state['cloudflared_files'] = $files.ToArray()
    $state['port'] = $hubPort
    Write-InstallState -Path $statePath -State $state
    Write-Ok ('Wrote {0}' -f $statePath)

    Show-AccessReminder -Fqdn $Fqdn -ConfigFile $configFile
    return 0
}

# ------------------------------------------------------------------------------ entry point
# Dot-sourcing (". .\setup-cloudflare.ps1") only loads the functions (used by tests).
if ($MyInvocation.InvocationName -ne '.') {
    if ($AdminAction) {
        exit (Invoke-AdminEntry)
    }
    try {
        $script:ExitCode = Invoke-CloudflareSetup
    } catch {
        Write-Host ''
        Write-Host ('Cloudflare setup stopped: {0}' -f (Get-RedactedText $_.Exception.Message)) -ForegroundColor Red
        Write-Host 'Fix the problem and re-run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1' -ForegroundColor Red
        $script:ExitCode = 1
    }
    exit $script:ExitCode
}
