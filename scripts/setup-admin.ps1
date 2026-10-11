<#
.SYNOPSIS
    Elevated part of the ShoutAbout setup: Windows Firewall rules (and, only as a fallback,
    the "ShoutAbout Hub" scheduled task).

.DESCRIPTION
    setup.ps1 starts this script once with "Run as administrator" (a single UAC prompt).
    You normally never run it yourself. It must run elevated.

    -Firewall:
      * Removes old rules in the firewall group "ShoutAbout" (e.g. for a previous port).
      * Creates the port rule  Name "ShoutAbout-Hub-TCP-<port>",
        DisplayName "ShoutAbout Hub (TCP <port>)", Group "ShoutAbout": Inbound, Allow, TCP,
        LocalPort <port>, Profile Private, RemoteAddress LocalSubnet.
      * Creates program+port rules for the BASE Python interpreter in -PythonDir
        (python.exe and pythonw.exe; the venv's Scripts\python.exe is only a redirector, the
        listening process is the base interpreter): "ShoutAbout-Hub-TCP-<port>-python" and
        "ShoutAbout-Hub-TCP-<port>-pythonw", same settings plus -Program. These stop the
        "Windows Security Alert - allow Python?" prompt from creating Block rules.
      * Detects enabled inbound BLOCK rules for python*.exe (Windows creates these when the
        "allow Python" prompt is cancelled; Block beats Allow), lists them and offers to remove
        them (asks first).
    -Task (fallback only; setup.ps1 registers the task without admin rights when it can):
      * Registers the scheduled task "ShoutAbout Hub" (folder \) that runs
        "<RepoRoot>\.venv\Scripts\pythonw.exe -m shoutabout run" at logon of -UserId,
        LogonType Interactive, RunLevel Limited (never elevated), 15 s logon delay, no time
        limit, restart on failure, priority 5.

    The outcome is written as JSON to -ResultFile ({"ok", "messages", "block_rules", ...})
    because the caller cannot capture the output of an elevated process. The window waits for
    Enter only when something failed, so the error can be read.

.PARAMETER Port
    TCP port the hub listens on (config.toml [server] port). Default 8765.

.PARAMETER RepoRoot
    Full path of the ShoutAbout repository (contains .venv). Required with -Task.

.PARAMETER UserId
    Account that runs the hub, as DOMAIN\user (setup.ps1 passes "$env:USERDOMAIN\$env:USERNAME").
    Required with -Task.

.PARAMETER PythonDir
    Directory of the base Python interpreter used by the venv (sys._base_executable's folder).
    When given, program+port allow rules are added for python.exe and pythonw.exe in it.

.PARAMETER Firewall
    Create/replace the firewall rules and check for Python block rules.

.PARAMETER Task
    Register (or replace) the "ShoutAbout Hub" scheduled task.

.PARAMETER ResultFile
    Path of the JSON result file to write.

.EXAMPLE
    # Normally started by setup.ps1. Manual use, from an elevated PowerShell:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-admin.ps1 -Firewall -Port 8765 -PythonDir "C:\Path\To\Python312"
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$RepoRoot = '',
    [string]$UserId = '',
    [string]$PythonDir = '',
    [switch]$Firewall,
    [switch]$Task,
    [string]$ResultFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue } catch { }

$TaskName = 'ShoutAbout Hub'
$FirewallGroup = 'ShoutAbout'
$script:Ok = $true
$script:Messages = New-Object System.Collections.Generic.List[string]
$script:BlockRules = New-Object System.Collections.Generic.List[object]
$script:RulesCreated = New-Object System.Collections.Generic.List[string]
$script:TaskRegistered = $false

function Add-Message {
    param([ValidateSet('OK', 'INFO', 'WARN', 'ERROR')][string]$Level, [string]$Text)
    $script:Messages.Add(('[{0}] {1}' -f $Level, $Text))
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERROR' { 'Red' } default { 'Gray' } }
    Write-Host ('  {0,-5} {1}' -f $Level, $Text) -ForegroundColor $color
    if ($Level -eq 'ERROR') { $script:Ok = $false }
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Save-Result {
    if (-not $ResultFile) { return }
    $result = [ordered]@{
        ok                 = [bool]$script:Ok
        messages           = $script:Messages.ToArray()
        block_rules        = $script:BlockRules.ToArray()
        firewall_rules     = $script:RulesCreated.ToArray()
        firewall_rule_name = ('ShoutAbout-Hub-TCP-{0}' -f $Port)
        task_registered    = [bool]$script:TaskRegistered
        task_name          = $TaskName
    }
    try {
        $json = ConvertTo-Json -InputObject $result -Depth 6
        [IO.File]::WriteAllText($ResultFile, $json, (New-Object System.Text.UTF8Encoding $false))
    } catch {
        Write-Host ('  Could not write the result file {0}: {1}' -f $ResultFile, $_.Exception.Message) -ForegroundColor Red
    }
}

function Get-PythonBlockRules {
    # Enabled inbound Block rules whose program is python*.exe (any folder).
    $found = New-Object System.Collections.Generic.List[object]
    $filters = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
            Where-Object { $_.Program -like '*\python*.exe' -or $_.Program -like 'python*.exe' })
    foreach ($f in $filters) {
        $rules = @($f | Get-NetFirewallRule -ErrorAction SilentlyContinue)
        foreach ($r in $rules) {
            if ([string]$r.Direction -eq 'Inbound' -and [string]$r.Action -eq 'Block' -and [string]$r.Enabled -eq 'True') {
                $safeName = ([string]$r.Name).Replace("'", "''")
                $found.Add([ordered]@{
                        name           = [string]$r.Name
                        display_name   = [string]$r.DisplayName
                        profile        = [string]$r.Profile
                        program        = [string]$f.Program
                        remove_command = ("Remove-NetFirewallRule -Name '{0}'" -f $safeName)
                    })
            }
        }
    }
    return $found.ToArray()
}

function Set-HubFirewallRules {
    $ruleName = 'ShoutAbout-Hub-TCP-{0}' -f $Port
    $display = 'ShoutAbout Hub (TCP {0})' -f $Port

    # Replace everything in our group (also removes rules for an old port) and any same-name rule.
    $old = @(Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue)
    foreach ($r in $old) {
        Remove-NetFirewallRule -Name $r.Name -ErrorAction Stop
        Add-Message INFO ('Removed old firewall rule {0}' -f $r.Name)
    }
    foreach ($n in @($ruleName, "$ruleName-python", "$ruleName-pythonw")) {
        $same = @(Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue)
        foreach ($r in $same) { Remove-NetFirewallRule -Name $r.Name -ErrorAction Stop }
    }

    $common = @{
        Group         = $FirewallGroup
        Direction     = 'Inbound'
        Action        = 'Allow'
        Protocol      = 'TCP'
        LocalPort     = [string]$Port
        Profile       = 'Private'
        RemoteAddress = 'LocalSubnet'
        Enabled       = 'True'
    }
    New-NetFirewallRule -Name $ruleName -DisplayName $display @common `
        -Description 'ShoutAbout dictation hub: iPhone on the home network -> this PC. Created by scripts\setup-admin.ps1.' |
        Out-Null
    $script:RulesCreated.Add($ruleName)
    Add-Message OK ('Firewall rule "{0}" (TCP {1}, Private networks, local subnet only)' -f $display, $Port)

    if ($PythonDir) {
        $dir = $PythonDir.TrimEnd('\')
        foreach ($exe in @('python', 'pythonw')) {
            $prog = Join-Path $dir ($exe + '.exe')
            if (-not (Test-Path -LiteralPath $prog)) {
                Add-Message WARN ('{0} not found; no program rule for it' -f $prog)
                continue
            }
            $n = '{0}-{1}' -f $ruleName, $exe
            New-NetFirewallRule -Name $n -DisplayName ('{0} - {1}.exe' -f $display, $exe) -Program $prog @common `
                -Description 'ShoutAbout dictation hub: allows the base Python interpreter on the hub port (Private networks).' |
                Out-Null
            $script:RulesCreated.Add($n)
            Add-Message OK ('Firewall rule for {0} on TCP {1}' -f $prog, $Port)
        }
    } else {
        Add-Message WARN 'No -PythonDir given: only the port rule was created (Windows may still ask to allow Python).'
    }

    # Block rules for python.exe override every Allow rule.
    $blocks = @(Get-PythonBlockRules)
    if ($blocks.Count -eq 0) {
        Add-Message OK 'No enabled inbound Block rules for python*.exe'
        return
    }
    Add-Message WARN ('{0} enabled inbound BLOCK rule(s) for Python found; Block rules override Allow rules:' -f $blocks.Count)
    foreach ($b in $blocks) {
        $script:BlockRules.Add($b)
        Write-Host ('        - "{0}"  profile={1}  program={2}' -f $b['display_name'], $b['profile'], $b['program']) -ForegroundColor Yellow
        Write-Host ('          remove: {0}' -f $b['remove_command']) -ForegroundColor Yellow
    }
    $answer = Read-Host '  Remove these Python block rules now? [y/N]'
    if ($answer -match '^(?i)(y|yes)$') {
        foreach ($b in $blocks) {
            try {
                Remove-NetFirewallRule -Name $b['name'] -ErrorAction Stop
                $b['removed'] = $true
                Add-Message OK ('Removed block rule "{0}"' -f $b['display_name'])
            } catch {
                $b['removed'] = $false
                Add-Message WARN ('Could not remove "{0}": {1}' -f $b['display_name'], $_.Exception.Message)
            }
        }
    } else {
        Add-Message WARN 'Block rules left in place. LAN dictation will fail until they are removed (commands above, as Administrator).'
    }
}

function Register-HubTask {
    if (-not $RepoRoot) { throw '-RepoRoot is required with -Task.' }
    $root = $RepoRoot.TrimEnd('\')
    $user = $UserId
    if (-not $user) {
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Add-Message WARN ('No -UserId given; registering the task for {0}' -f $user)
    }
    $pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) {
        Add-Message WARN ('{0} does not exist yet; the task will work once the venv is created.' -f $pythonw)
    }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m shoutabout run' -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT15S'   # give Wi-Fi a moment after logon
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    $settings.Priority = 5     # default 7 is "background" (below-normal CPU and I/O priority)
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force `
        -Description 'ShoutAbout dictation hub (python -m shoutabout run). Created by scripts\setup.ps1.' | Out-Null
    $script:TaskRegistered = $true
    Add-Message OK ('Scheduled task "{0}" registered for {1} (at logon, not elevated)' -f $TaskName, $user)
}

# ------------------------------------------------------------------------------------ main
Write-Host ''
Write-Host 'ShoutAbout setup (administrator step)' -ForegroundColor Cyan

if (-not (Test-IsAdmin)) {
    Add-Message ERROR 'setup-admin.ps1 must run as Administrator. It is started by setup.ps1 with a UAC prompt; run setup.ps1 instead.'
    Save-Result
    exit 1
}

if (-not $Firewall -and -not $Task) {
    Add-Message INFO 'Nothing to do (pass -Firewall and/or -Task).'
    Save-Result
    exit 0
}

if ($Firewall) {
    Write-Host ''
    Write-Host '[firewall] Windows Firewall rules' -ForegroundColor Cyan
    try { Set-HubFirewallRules } catch { Add-Message ERROR ('Firewall: {0}' -f $_.Exception.Message) }
}

if ($Task) {
    Write-Host ''
    Write-Host '[task] Scheduled task' -ForegroundColor Cyan
    try { Register-HubTask } catch { Add-Message ERROR ('Scheduled task: {0}' -f $_.Exception.Message) }
}

Save-Result
if (-not $script:Ok) {
    Write-Host ''
    Write-Host 'Something failed (see the messages above).' -ForegroundColor Red
    [void](Read-Host 'Press Enter to close')
    exit 1
}
Write-Host ''
Write-Host 'Administrator step finished.' -ForegroundColor Green
exit 0
