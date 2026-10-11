<#
.SYNOPSIS
    Clicks through the installer's interactive wizard and the uninstall dialog with the
    keyboard and saves a screenshot of every page. For CI and throwaway VMs only.

.DESCRIPTION
    Run elevated, in Windows PowerShell 5.1, on a machine where ShoutAbout isn't installed:

        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\wizard-screenshots.ps1 `
            -Installer .\packaging\dist\ShoutAbout-Setup-0.1.0.exe -OutDir .\shots

    1. Starts the installer without /SILENT: Welcome, the start-at-sign-in page, the
       installation, the Finished page (Enter = Finish, which starts ShoutAbout).
    2. Runs the uninstaller the way Windows Settings does (unins000.exe /SILENT): the
       one-question dialog, then Enter = Uninstall (the box stays unticked).
    A screenshot of the whole screen is saved after each step. Pages are driven with Enter
    only (the default buttons), so this also proves the wizard can be finished without a
    mouse. ShoutAbout runs with a throwaway SHOUTABOUT_HOME (loopback, tiny model).

    It refuses to run outside GitHub Actions unless -Force is given.

.PARAMETER Installer
    Path to ShoutAbout-Setup-<version>.exe.

.PARAMETER OutDir
    Folder for the PNG screenshots.

.PARAMETER Force
    Run although this isn't GitHub Actions (a throwaway VM).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $Force -and $env:GITHUB_ACTIONS -ne 'true') {
    throw 'This script installs and uninstalls ShoutAbout. Run it in CI or on a throwaway VM with -Force.'
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Installer = (Resolve-Path -LiteralPath $Installer).Path
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$AppDir = Join-Path $env:ProgramFiles 'ShoutAbout'
$work = Join-Path ([IO.Path]::GetTempPath()) 'shoutabout-wizard'
$homeDir = Join-Path $work 'home'
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
$models = (Join-Path $work 'models').Replace('\', '/')
Set-Content -LiteralPath (Join-Path $homeDir 'config.toml') -Encoding ASCII -Value @"
[server]
host = "127.0.0.1"
port = 18766

[stt]
model = "tiny.en"
device = "cpu"
download_root = "$models"

[cleanup]
enabled = false

[learn]
enabled = false
"@
$env:SHOUTABOUT_HOME = $homeDir
$shell = New-Object -ComObject WScript.Shell
$script:Shot = 0

function Save-Screen([string]$Name) {
    $script:Shot++
    $path = Join-Path $OutDir ('{0:D2}-{1}.png' -f $script:Shot, $Name)
    try {
        $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        try {
            $g.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
            $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
        } finally { $g.Dispose(); $bmp.Dispose() }
        Write-Host ('  saved {0}' -f $path)
    } catch {
        Write-Host ('  screenshot {0} failed: {1}' -f $Name, $_.Exception.Message)
    }
}

function Wait-Window([string]$Title, [int]$Seconds) {
    # AppActivate finds any top-level window by its title (Inno Setup's dialogs aren't
    # always their process's "main window").
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if ($shell.AppActivate($Title)) { return }
        Start-Sleep -Milliseconds 500
    }
    throw ('No window titled "{0}" after {1} s' -f $Title, $Seconds)
}

function Test-InstallDone {
    # The installer's last step rewrites UninstallString (it ends in /SILENT); the Finished
    # page comes right after.
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{B70711A6-7F23-457D-B910-31004776AB13}_is1'
    $entry = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    return ($null -ne $entry -and [string]$entry.UninstallString -like '* /SILENT')
}

function Send-Enter([string]$Title) {
    [void]$shell.AppActivate($Title)
    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return [bool](& $Condition)
}

try {
    Write-Host '== Install wizard'
    $setup = Start-Process -FilePath $Installer -PassThru
    $title = 'ShoutAbout Setup'
    Wait-Window $title 60
    Start-Sleep -Seconds 2
    Save-Screen 'welcome'
    Send-Enter $title
    Start-Sleep -Seconds 2
    Save-Screen 'start-at-sign-in'
    Send-Enter $title
    Start-Sleep -Seconds 3
    Save-Screen 'installing'
    if (-not (Wait-Until { Test-InstallDone } 300)) { throw 'install did not finish' }
    Start-Sleep -Seconds 4
    Save-Screen 'finished'
    Send-Enter $title
    [void](Wait-Until { $setup.HasExited } 60)
    Start-Sleep -Seconds 8
    Save-Screen 'running'

    Write-Host '== Uninstall dialog (as Windows Settings runs it)'
    $uninstaller = Join-Path $AppDir 'unins000.exe'
    Start-Process -FilePath $uninstaller -ArgumentList '/SILENT' | Out-Null
    $title = 'Uninstall ShoutAbout'
    Wait-Window $title 60
    Start-Sleep -Seconds 2
    Save-Screen 'uninstall-dialog'
    Send-Enter $title
    Start-Sleep -Seconds 2
    Save-Screen 'uninstalling'
    if (-not (Wait-Until { -not (Test-Path -LiteralPath $AppDir) } 180)) { throw 'uninstall did not finish' }
    Save-Screen 'removed'
    $data = Join-Path $env:APPDATA 'shoutabout'
    Write-Host ('Uninstalled. %APPDATA%\shoutabout kept: {0}' -f (Test-Path -LiteralPath $data))
} finally {
    $left = Join-Path $AppDir 'unins000.exe'
    if (Test-Path -LiteralPath $left) {
        Write-Host 'Cleaning up a leftover install'
        Start-Process -FilePath $left -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait | Out-Null
    }
}
