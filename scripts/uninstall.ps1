<#
.SYNOPSIS
    Removes the ShoutAbout hub from this PC (task, firewall rules; optionally tunnel, models,
    data and the venv).

.DESCRIPTION
    Run from a normal (not elevated) PowerShell in the repository root:

        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1

    Always:
      * stops the running hub,
      * unregisters the "ShoutAbout Hub" scheduled task,
      * removes every firewall rule in the group "ShoutAbout" (one UAC prompt, only if any exist).
    Optional:
      -RemoveTunnel  removes only what setup created, according to install-state.json:
                     local mode: the cloudflared service it installed, its files in
                     C:\Windows\System32\config\systemprofile\.cloudflared and the tunnel
                     ("cloudflared tunnel delete"); remote mode: the service it installed.
                     A tunnel/service that already existed ("existing" mode) is never touched;
                     the dashboard clean-up steps are printed instead.
      -RemoveModels  "ollama rm <cleanup model>" and the Whisper model folders in the Hugging
                     Face cache (models--*--faster-whisper-large-v3-turbo).
      -RemoveData    deletes the config dir (%APPDATA%\shoutabout): config, vocab, history, logs
                     and the hub token. Asks first unless -Yes.
      -RemoveVenv    deletes the repository's .venv.
    Ollama, cloudflared and uv themselves are never uninstalled; the winget commands are printed.

.PARAMETER RemoveData
    Delete the config dir (history, vocab, config and the hub token).

.PARAMETER RemoveTunnel
    Remove the Cloudflare pieces that setup created (see the description).

.PARAMETER RemoveModels
    Remove the Ollama cleanup model and the cached Whisper model.

.PARAMETER RemoveVenv
    Delete .venv in the repository.

.PARAMETER All
    Same as -RemoveData -RemoveTunnel -RemoveModels -RemoveVenv.

.PARAMETER Yes
    Do not ask for confirmation.

.PARAMETER AdminPlan
    Internal: JSON plan for the elevated step. Do not use.

.PARAMETER ResultFile
    Internal: JSON result file of the elevated step. Do not use.

.EXAMPLE
    # Remove the task and firewall rules only (keeps config, models, tunnel):
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1

.EXAMPLE
    # Remove everything ShoutAbout created, without prompts:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -All -Yes
#>
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [switch]$RemoveTunnel,
    [switch]$RemoveModels,
    [switch]$RemoveVenv,
    [switch]$All,
    [switch]$Yes,
    [string]$AdminPlan = '',
    [string]$ResultFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue } catch { }

$script:SelfPath = $PSCommandPath
$script:StepNo = 0
$script:StepTotal = 7
$script:LastExit = 0
$script:ExitCode = 0
$script:Ok = $true
$script:Messages = New-Object System.Collections.Generic.List[string]
$script:FirewallGroup = 'ShoutAbout'
$script:DefaultTaskName = 'ShoutAbout Hub'
$script:WhisperRepos = @(
    'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo',
    'models--dropbox-dash--faster-whisper-large-v3-turbo'
)

# ------------------------------------------------------------------------------ helpers
function Get-RedactedText([string]$Text) {
    if (-not $Text) { return $Text }
    $t = [regex]::Replace($Text, '(?im)(--token(?:-file)?)(?:\s*=\s*|\s+).*$', '$1 <redacted>')
    $t = [regex]::Replace($t, 'eyJ[A-Za-z0-9_\-+/=.]{8,}', '<redacted>')
    return $t
}
function Write-Step([string]$Title) {
    $script:StepNo++
    Write-Host ''
    Write-Host ('[{0}/{1}] {2}' -f $script:StepNo, $script:StepTotal, $Title) -ForegroundColor Cyan
}
function Write-Ok([string]$Text) { Write-Host ('  OK    {0}' -f $Text) -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host ('  ..    {0}' -f $Text) }
function Write-Skip([string]$Text) { Write-Host ('  SKIP  {0}' -f $Text) -ForegroundColor DarkGray }
function Write-Warn([string]$Text) { Write-Host ('  WARN  {0}' -f $Text) -ForegroundColor Yellow }
function Write-Text([string]$Text) { Write-Host ('        {0}' -f $Text) }
function Add-Message {
    param([ValidateSet('OK', 'INFO', 'WARN', 'ERROR')][string]$Level, [string]$Text)
    $clean = Get-RedactedText $Text
    $script:Messages.Add(('[{0}] {1}' -f $Level, $clean))
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERROR' { 'Red' } default { 'Gray' } }
    Write-Host ('  {0,-5} {1}' -f $Level, $clean) -ForegroundColor $color
    if ($Level -eq 'ERROR') { $script:Ok = $false }
}

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
        throw ('"{0} {1}" failed with exit code {2}.' -f (Split-Path -Leaf $FilePath), ($Arguments -join ' '), $script:LastExit)
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
        Write-Warn ('{0} is not valid JSON; ignoring it.' -f $Path)
    }
    return [ordered]@{}
}

function Write-InstallState([string]$Path, [System.Collections.IDictionary]$State) {
    $State['updated'] = (Get-Date).ToString("yyyy-MM-dd'T'HH:mm:sszzz")
    $json = ConvertTo-Json -InputObject $State -Depth 10
    [IO.File]::WriteAllText($Path, $json + "`n", (New-Object System.Text.UTF8Encoding $false))
}

function Get-PowerShellExe {
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        return (Join-Path $env:windir 'Sysnative\WindowsPowerShell\v1.0\powershell.exe')
    }
    return (Join-Path $env:windir 'System32\WindowsPowerShell\v1.0\powershell.exe')
}

function Get-SystemCloudflaredDir {
    return (Join-Path $env:windir 'System32\config\systemprofile\.cloudflared')
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

function Find-Ollama {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $cmd) { return $cmd.Source }
    $p = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    return $null
}

function Get-HubProcesses {
    return @(Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+shoutabout\s+run' })
}

function Get-TomlValue([string]$File, [string]$Section, [string]$Key) {
    # Minimal reader for a quoted string value (used only when the venv is gone).
    if (-not (Test-Path -LiteralPath $File)) { return $null }
    $inSection = $false
    foreach ($l in [IO.File]::ReadAllLines($File)) {
        if ($l -match '^\s*\[([^\]]+)\]') { $inSection = ($Matches[1].Trim() -eq $Section); continue }
        if ($inSection -and $l -match ('^\s*{0}\s*=\s*"([^"]*)"' -f [regex]::Escape($Key))) { return $Matches[1] }
    }
    return $null
}

function Get-HfHubCache {
    if ($env:HF_HUB_CACHE) { return $env:HF_HUB_CACHE }
    if ($env:HF_HOME) { return (Join-Path $env:HF_HOME 'hub') }
    return (Join-Path $env:USERPROFILE '.cache\huggingface\hub')
}

function Confirm-Action([string]$Question) {
    if ($Yes) { return $true }
    $a = Read-Host ('  {0} [y/N]' -f $Question)
    return ($a -match '^(?i)(y|yes)$')
}

# ------------------------------------------------------------------------------ elevated step
function Invoke-AdminEntry {
    Write-Host ''
    Write-Host 'ShoutAbout uninstall (administrator step)' -ForegroundColor Cyan
    try {
        if (-not (Test-IsAdmin)) { throw 'This step must run as Administrator.' }
        $plan = [IO.File]::ReadAllText($AdminPlan) | ConvertFrom-Json

        if ([bool](Get-Prop $plan 'unregister_task' $false)) {
            $tn = [string](Get-Prop $plan 'task_name' $script:DefaultTaskName)
            $t = Get-ScheduledTask -TaskName $tn -TaskPath '\' -ErrorAction SilentlyContinue
            if ($null -ne $t) {
                try { Stop-ScheduledTask -TaskName $tn -TaskPath '\' -ErrorAction SilentlyContinue } catch { }
                Unregister-ScheduledTask -TaskName $tn -TaskPath '\' -Confirm:$false -ErrorAction Stop
                Add-Message OK ('Unregistered scheduled task "{0}".' -f $tn)
            }
        }

        $rules = New-Object System.Collections.Generic.List[string]
        foreach ($r in @(Get-NetFirewallRule -Group $script:FirewallGroup -ErrorAction SilentlyContinue)) { $rules.Add([string]$r.Name) }
        $named = [string](Get-Prop $plan 'firewall_rule_name' '')
        if ($named) {
            foreach ($n in @($named, "$named-python", "$named-pythonw")) {
                foreach ($r in @(Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue)) {
                    if (-not $rules.Contains([string]$r.Name)) { $rules.Add([string]$r.Name) }
                }
            }
        }
        foreach ($n in $rules) {
            try { Remove-NetFirewallRule -Name $n -ErrorAction Stop; Add-Message OK ('Removed firewall rule {0}' -f $n) }
            catch { Add-Message ERROR ('Could not remove firewall rule {0}: {1}' -f $n, $_.Exception.Message) }
        }
        if ($rules.Count -eq 0) { Add-Message INFO 'No ShoutAbout firewall rules found.' }

        if ([bool](Get-Prop $plan 'remove_service' $false)) {
            $svcName = [string](Get-Prop $plan 'service_name' '')
            $exe = [string](Get-Prop $plan 'cloudflared_exe' '')
            if ($svcName -and $null -ne (Get-Service -Name $svcName -ErrorAction SilentlyContinue)) {
                if ($svcName -eq 'cloudflared' -and $exe -and (Test-Path -LiteralPath $exe)) {
                    $r = Invoke-ExeCapture -FilePath $exe -Arguments @('service', 'uninstall')
                    foreach ($l in (@($r.StdOut) + @($r.StdErr))) { if ($l) { Write-Host ('        {0}' -f (Get-RedactedText $l)) } }
                }
                if ($null -ne (Get-Service -Name $svcName -ErrorAction SilentlyContinue)) {
                    & sc.exe stop $svcName | Out-Null
                    Start-Sleep -Seconds 2
                    & sc.exe delete $svcName | Out-Null
                }
                if ($null -eq (Get-Service -Name $svcName -ErrorAction SilentlyContinue)) { Add-Message OK ('Removed service {0}.' -f $svcName) }
                else { Add-Message WARN ('Service {0} is marked for deletion; it disappears after a reboot.' -f $svcName) }
            } else {
                Add-Message INFO ('Service {0} not found.' -f $svcName)
            }
        }

        $sysDir = (Get-SystemCloudflaredDir).TrimEnd('\') + '\'
        foreach ($f in @(Get-Prop $plan 'files' @())) {
            $path = [string]$f
            if (-not $path) { continue }
            if (-not $path.StartsWith($sysDir, [StringComparison]::OrdinalIgnoreCase)) {
                Add-Message WARN ('Not deleting {0} (outside {1}).' -f $path, $sysDir)
                continue
            }
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force -ErrorAction Stop
                Add-Message OK ('Deleted {0}' -f $path)
            }
        }
    } catch {
        Add-Message ERROR $_.Exception.Message
    }
    if ($ResultFile) {
        $result = [ordered]@{ ok = [bool]$script:Ok; messages = $script:Messages.ToArray() }
        [IO.File]::WriteAllText($ResultFile, (ConvertTo-Json -InputObject $result -Depth 4), (New-Object System.Text.UTF8Encoding $false))
    }
    if (-not $script:Ok) {
        [void](Read-Host 'Press Enter to close')
        return 1
    }
    return 0
}

# ------------------------------------------------------------------------------ main
function Invoke-Uninstall {
    $doData = [bool]($RemoveData -or $All)
    $doTunnel = [bool]($RemoveTunnel -or $All)
    $doModels = [bool]($RemoveModels -or $All)
    $doVenv = [bool]($RemoveVenv -or $All)

    Write-Host 'ShoutAbout uninstall' -ForegroundColor White

    # ------------------------------------------------------------------ 1. plan
    Write-Step 'What will be removed'
    $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath.TrimEnd('\')
    $configHome = Get-ConfigHome
    $statePath = Join-Path $configHome 'install-state.json'
    $state = Read-InstallState $statePath
    $created = Get-Prop $state 'created' $null
    $taskName = [string](Get-Prop $state 'task_name' $script:DefaultTaskName)
    $ruleName = [string](Get-Prop $state 'firewall_rule_name' '')
    $tunnelMode = [string](Get-Prop $state 'tunnel_mode' 'none')
    $tunnelName = [string](Get-Prop $state 'tunnel_name' '')
    $tunnelId = [string](Get-Prop $state 'tunnel_id' '')
    $fqdn = [string](Get-Prop $state 'public_hostname' '')
    $svcName = [string](Get-Prop $state 'cloudflared_service_name' 'cloudflared')
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

    # Cleanup model name: ask the CLI while the venv still exists, else read config.toml.
    $cleanupModel = $null
    if ($doModels) {
        if (Test-Path -LiteralPath $venvPython) {
            $r = Invoke-ExeCapture -FilePath $venvPython -Arguments @('-m', 'shoutabout', 'info', '--json')
            if ($r.ExitCode -eq 0) { try { $cleanupModel = [string](Get-Prop ((@($r.StdOut) -join "`n") | ConvertFrom-Json) 'cleanup_model' '') } catch { } }
        }
        if (-not $cleanupModel) { $cleanupModel = Get-TomlValue (Join-Path $configHome 'config.toml') 'cleanup' 'model' }
    }

    Write-Text '- stop the hub, remove the "ShoutAbout Hub" scheduled task and the "ShoutAbout" firewall rules'
    if ($doTunnel) { Write-Text ('- Cloudflare pieces created by setup (tunnel mode: {0})' -f $tunnelMode) }
    if ($doModels) { Write-Text ('- models: Ollama "{0}" and the cached Whisper model' -f $(if ($cleanupModel) { $cleanupModel } else { '(unknown)' })) }
    if ($doData) { Write-Text ('- ALL data in {0} (history, vocab, config, hub token)' -f $configHome) }
    if ($doVenv) { Write-Text ('- {0}' -f (Join-Path $RepoRoot '.venv')) }
    if (-not (Confirm-Action 'Continue?')) { Write-Host 'Nothing was changed.'; return 1 }

    # ------------------------------------------------------------------ 2. stop
    Write-Step 'Stop the hub'
    try { Stop-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop } catch { }
    $procs = @(Get-HubProcesses)
    foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { } }
    if ($procs.Count -gt 0) { Start-Sleep -Seconds 1; Write-Ok ('Stopped {0} hub process(es).' -f $procs.Count) } else { Write-Ok 'Hub not running.' }

    # ------------------------------------------------------------------ 3. task
    Write-Step 'Scheduled task'
    $taskNeedsAdmin = $false
    $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Ok 'No scheduled task.'
    } else {
        try {
            Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
            Write-Ok ('Unregistered "{0}".' -f $taskName)
        } catch {
            Write-Info 'The task was created elevated; removing it in the administrator step.'
            $taskNeedsAdmin = $true
        }
    }

    # ------------------------------------------------------------------ 4. admin
    Write-Step 'Administrator step (firewall; tunnel service if requested)'
    $haveRules = (@(Get-NetFirewallRule -Group $script:FirewallGroup -ErrorAction SilentlyContinue).Count -gt 0)
    if (-not $haveRules -and $ruleName) { $haveRules = ($null -ne (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) }
    $oursMode = ($tunnelMode -eq 'local' -or $tunnelMode -eq 'remote')
    $removeService = ($doTunnel -and $oursMode -and [bool](Get-Prop $created 'cloudflared_service' $false) -and
        $null -ne (Get-Service -Name $svcName -ErrorAction SilentlyContinue))
    $files = @()
    if ($doTunnel -and $oursMode) { $files = @(Get-Prop $state 'cloudflared_files' @() | Where-Object { $_ }) }
    $adminOk = $true
    if (-not $haveRules -and -not $taskNeedsAdmin -and -not $removeService -and $files.Count -eq 0) {
        Write-Ok 'Nothing needs admin rights (no firewall rules, no service to remove).'
    } else {
        $plan = [ordered]@{
            unregister_task    = $taskNeedsAdmin
            task_name          = $taskName
            firewall_rule_name = $ruleName
            remove_service     = $removeService
            service_name       = $svcName
            cloudflared_exe    = $(if ($removeService) { Find-Cloudflared } else { $null })
            files              = $files
        }
        $planFile = Join-Path $env:TEMP ('shoutabout-uninstall-{0}.json' -f [guid]::NewGuid().ToString('N'))
        $resultFile = Join-Path $env:TEMP ('shoutabout-uninstall-{0}.result.json' -f [guid]::NewGuid().ToString('N'))
        [IO.File]::WriteAllText($planFile, (ConvertTo-Json -InputObject $plan -Depth 4), (New-Object System.Text.UTF8Encoding $false))
        $argText = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -AdminPlan "{1}" -ResultFile "{2}"' -f $script:SelfPath, $planFile, $resultFile
        Write-Info 'Opening the administrator step (approve the UAC prompt)...'
        try {
            Start-Process -FilePath (Get-PowerShellExe) -Verb RunAs -Wait -ArgumentList $argText | Out-Null
            if (Test-Path -LiteralPath $resultFile) {
                $res = [IO.File]::ReadAllText($resultFile) | ConvertFrom-Json
                foreach ($m in @(Get-Prop $res 'messages' @())) { Write-Info ([string]$m) }
                $adminOk = [bool](Get-Prop $res 'ok' $false)
            } else {
                $adminOk = $false
                Write-Warn 'The administrator step did not report back.'
            }
        } catch {
            $adminOk = $false
            Write-Warn ('Administrator step not run: {0}' -f $_.Exception.Message)
        } finally {
            Remove-Item -LiteralPath $planFile, $resultFile -Force -ErrorAction SilentlyContinue
        }
        if (-not $adminOk) {
            $script:ExitCode = 1
            Write-Warn ('Remove the firewall rules manually (admin PowerShell): Get-NetFirewallRule -Group {0} | Remove-NetFirewallRule' -f $script:FirewallGroup)
        }
    }

    # ------------------------------------------------------------------ 5. tunnel
    Write-Step 'Cloudflare Tunnel'
    $hostLabel = if ($fqdn) { $fqdn } else { '<your hostname>' }
    if ($tunnelMode -eq 'none' -or -not $tunnelMode) {
        Write-Ok 'Setup did not configure a tunnel.'
    } elseif (-not $doTunnel) {
        Write-Skip ('Tunnel left in place (mode {0}). Use -RemoveTunnel to remove what setup created.' -f $tunnelMode)
    } else {
        if ($tunnelMode -eq 'local' -and [bool](Get-Prop $created 'tunnel' $false)) {
            $exe = Find-Cloudflared
            $ref = if ($tunnelId) { $tunnelId } else { $tunnelName }
            if ($null -ne $exe -and $ref -and $adminOk) {
                Invoke-Exe -FilePath $exe -Arguments @('tunnel', 'delete', '-f', $ref) -AllowFailure
                if ($script:LastExit -eq 0) {
                    Write-Ok ('Deleted tunnel {0}.' -f $ref)
                    $cfDir = Join-Path $env:USERPROFILE '.cloudflared'
                    foreach ($f in @((Join-Path $cfDir ('{0}.json' -f $tunnelId)), (Join-Path $cfDir 'shoutabout-staging.yml'))) {
                        if ($tunnelId -and (Test-Path -LiteralPath $f)) { Remove-Item -LiteralPath $f -Force; Write-Ok ('Deleted {0}' -f $f) }
                    }
                } else {
                    Write-Warn ('Could not delete the tunnel. Later: cloudflared tunnel delete -f {0}' -f $ref)
                }
            } else {
                Write-Warn ('Tunnel not deleted (service still present or cloudflared missing). Later: cloudflared tunnel delete -f {0}' -f $ref)
            }
            if ([bool](Get-Prop $created 'dns_route' $false)) {
                Write-Text ('Delete the DNS record: Cloudflare dashboard > your domain > DNS > Records > CNAME {0}' -f $hostLabel)
            }
            Write-Text '%USERPROFILE%\.cloudflared\cert.pem was kept (it may be used for other tunnels).'
        } elseif ($tunnelMode -eq 'remote') {
            Write-Text ('Delete the tunnel "{0}" in the dashboard: Networking > Tunnels > {0} > Delete' -f $(if ($tunnelName) { $tunnelName } else { 'shoutabout' }))
            Write-Text ('(this removes its routes), then delete the DNS record {0} if it remains (DNS > Records).' -f $hostLabel)
        } else {
            Write-Info 'This tunnel existed before ShoutAbout; it is NOT touched. In the dashboard, remove only:'
            if ($tunnelMode -eq 'existing-local') {
                Write-Text ('- the ingress rule for {0} in the tunnel config file (then Restart-Service cloudflared as admin)' -f $hostLabel)
            } else {
                Write-Text ('- Networking > Tunnels > <your tunnel> > Routes: the published application route {0}' -f $hostLabel)
            }
            Write-Text ('- DNS > Records: the CNAME {0} if it remains' -f $hostLabel)
        }
        Write-Text 'Cloudflare Access clean-up (Zero Trust > Access controls):'
        Write-Text ('- Applications: the self-hosted application for {0}' -f $hostLabel)
        Write-Text '- Policies: the ShoutAbout Service Auth policy'
        Write-Text '- Service credentials > Service Tokens: the token used by the iPhone'
        if (-not $doData -and (Test-Path -LiteralPath $statePath)) {
            $state['tunnel_mode'] = 'none'
            $state['tunnel_name'] = $null
            $state['tunnel_id'] = $null
            $state['cloudflared_files'] = @()
            $state['created'] = [ordered]@{ tunnel = $false; dns_route = $false; cloudflared_service = $false; cloudflared_config = $false }
            Write-InstallState -Path $statePath -State $state
        }
    }

    # ------------------------------------------------------------------ 6. models
    Write-Step 'Models'
    if (-not $doModels) {
        Write-Skip 'Models kept (use -RemoveModels).'
    } else {
        $ollama = Find-Ollama
        if ($cleanupModel -and $null -ne $ollama) {
            Invoke-Exe -FilePath $ollama -Arguments @('rm', $cleanupModel) -AllowFailure
            if ($script:LastExit -eq 0) { Write-Ok ('Removed Ollama model {0}.' -f $cleanupModel) }
            else { Write-Warn ('ollama rm failed (is Ollama running?). Later: ollama rm {0}' -f $cleanupModel) }
        } elseif ($cleanupModel) {
            Write-Skip ('Ollama not found; nothing to remove for {0}.' -f $cleanupModel)
        } else {
            Write-Warn 'Cleanup model name unknown; list models with "ollama list" and remove with "ollama rm <name>".'
        }
        $hub = Get-HfHubCache
        foreach ($repo in $script:WhisperRepos) {
            foreach ($d in @((Join-Path $hub $repo), (Join-Path $hub ('.locks\{0}' -f $repo)))) {
                if (Test-Path -LiteralPath $d) {
                    Remove-Item -LiteralPath $d -Recurse -Force
                    Write-Ok ('Deleted {0}' -f $d)
                }
            }
        }
        Write-Text 'A custom [stt] download_root in config.toml is not cleaned automatically.'
    }

    # ------------------------------------------------------------------ 7. data + venv
    Write-Step 'Data and venv'
    if (-not $doData) {
        Write-Skip ('Kept {0} (use -RemoveData).' -f $configHome)
    } elseif (-not (Test-Path -LiteralPath $configHome)) {
        Write-Ok 'Config dir already gone.'
    } else {
        Write-Warn ('{0} contains your dictation history, vocab, config and the hub token.' -f $configHome)
        if (Confirm-Action 'Delete it permanently?') {
            Remove-Item -LiteralPath $configHome -Recurse -Force
            Write-Ok ('Deleted {0}' -f $configHome)
        } else {
            Write-Skip 'Kept the config dir.'
        }
    }
    $venv = Join-Path $RepoRoot '.venv'
    if (-not $doVenv) {
        Write-Skip 'Kept .venv (use -RemoveVenv).'
    } elseif (Test-Path -LiteralPath $venv) {
        Remove-Item -LiteralPath $venv -Recurse -Force
        Write-Ok ('Deleted {0}' -f $venv)
    }

    Write-Host ''
    Write-Host 'Not removed (shared tools; remove them yourself if nothing else needs them):' -ForegroundColor Cyan
    Write-Text 'Ollama app       : winget uninstall --id Ollama.Ollama -e'
    Write-Text 'cloudflared      : winget uninstall --id Cloudflare.cloudflared -e   (not if another tunnel service uses it)'
    Write-Text 'uv               : winget uninstall --id astral-sh.uv -e   (uv cache clean / uv python uninstall --all first)'
    Write-Text ('This repository  : {0}' -f $RepoRoot)
    return $script:ExitCode
}

# ------------------------------------------------------------------------------ entry point
if ($MyInvocation.InvocationName -ne '.') {
    if ($AdminPlan) { exit (Invoke-AdminEntry) }
    try {
        $script:ExitCode = Invoke-Uninstall
    } catch {
        Write-Host ''
        Write-Host ('Uninstall stopped at step {0}/{1}: {2}' -f $script:StepNo, $script:StepTotal, $_.Exception.Message) -ForegroundColor Red
        $script:ExitCode = 1
    }
    exit $script:ExitCode
}
