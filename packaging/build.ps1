<#
.SYNOPSIS
    Builds ShoutAbout.exe (PyInstaller, onedir) and, if Inno Setup 6 is available, the
    one-click installer ShoutAbout-Setup-<version>.exe.

.DESCRIPTION
    Run from the repository root after preparing the build venv:

        uv sync --locked --extra cuda --group packaging
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\build.ps1

    Steps:
      1. Reads the version from the shoutabout package.
      2. Renders the icon, the wizard images (packaging\make_assets.py).
      3. Runs PyInstaller with packaging\shoutabout.spec -> packaging\dist\ShoutAbout\, then
         fails if a forbidden file is in it (packaging\bundle_rules.py: no ASIO builds, no
         PyAV/FFmpeg).
      4. Writes packaging\dist\size-report.txt (biggest folders and files).
      5. Unless -SkipInstaller: compiles packaging\installer.iss with ISCC ->
         packaging\dist\ShoutAbout-Setup-<version>.exe.

    Never installs anything and never runs the app.

    Code signing (optional; skipped when nothing is configured, so builds are unsigned):
      SHOUTABOUT_SIGN_PFX_BASE64 + SHOUTABOUT_SIGN_PFX_PASSWORD
          a base64 .pfx code-signing certificate; imported into CurrentUser\My for the build
          and removed again afterwards;
      SHOUTABOUT_SIGN_THUMBPRINT
          or the SHA-1 thumbprint of a certificate already in CurrentUser\My (e.g. on a
          hardware token);
      SHOUTABOUT_SIGN_TIMESTAMP_URL
          RFC 3161 timestamp server (default http://timestamp.digicert.com).
    Inno Setup then signs ShoutAbout.exe, the installer and the uninstaller with signtool.exe
    (from the Windows SDK).

.PARAMETER Python
    The build venv's python.exe. Default: .venv\Scripts\python.exe in the repository.

.PARAMETER Iscc
    Path to ISCC.exe. Default: $env:ISCC, then PATH, then Program Files (x86)\Inno Setup 6.

.PARAMETER SkipInstaller
    Only build the PyInstaller folder (no Inno Setup needed).

.PARAMETER RequireInstaller
    Fail instead of warning when Inno Setup isn't found.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\build.ps1 -SkipInstaller
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$Iscc = '',
    [switch]$SkipInstaller,
    [switch]$RequireInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Packaging = $PSScriptRoot
$Repo = Split-Path -Parent $Packaging
$Dist = Join-Path $Packaging 'dist'
$Work = Join-Path $Packaging 'build'
$Assets = Join-Path $Work 'assets'
$AppDir = Join-Path $Dist 'ShoutAbout'

function Write-Step([string]$Text) { Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan }

function Invoke-Checked {
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$Arguments = @())
    # Native tools log to stderr; under Windows PowerShell 5.1 that must not count as an error.
    $ErrorActionPreference = 'Continue'
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ('"{0}" failed with exit code {1}' -f (Split-Path -Leaf $FilePath), $LASTEXITCODE)
    }
}

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return '{0:N2} GB' -f ($Bytes / 1GB) }
    return '{0:N1} MB' -f ($Bytes / 1MB)
}

function Get-FolderBytes([string]$Path) {
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [double]$sum
}

function Find-Iscc {
    if ($Iscc) { return $Iscc }
    if ($env:ISCC -and (Test-Path -LiteralPath $env:ISCC)) { return $env:ISCC }
    $cmd = Get-Command 'iscc.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
        if (-not $base) { continue }
        $candidate = Join-Path $base 'Inno Setup 6\ISCC.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return ''
}

function Find-SignTool {
    $cmd = Get-Command 'signtool.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path -LiteralPath $kits) {
        $found = Get-ChildItem -LiteralPath $kits -Recurse -Filter 'signtool.exe' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return ''
}

function Write-SizeReport {
    $report = New-Object System.Collections.Generic.List[string]
    $internal = Join-Path $AppDir '_internal'
    $total = Get-FolderBytes $AppDir
    $report.Add(('ShoutAbout {0}: {1} in {2}' -f $script:Version, (Format-Size $total), $AppDir))
    $report.Add('')
    $report.Add('Largest folders in _internal:')
    $rows = foreach ($d in Get-ChildItem -LiteralPath $internal -Directory -Force) {
        [pscustomobject]@{ Name = $d.Name; Bytes = (Get-FolderBytes $d.FullName) }
    }
    foreach ($r in ($rows | Sort-Object Bytes -Descending | Select-Object -First 15)) {
        $report.Add(('  {0,10}  {1}' -f (Format-Size $r.Bytes), $r.Name))
    }
    $report.Add('')
    $report.Add('Largest files:')
    $files = Get-ChildItem -LiteralPath $AppDir -Recurse -File -Force | Sort-Object Length -Descending | Select-Object -First 15
    foreach ($f in $files) {
        $report.Add(('  {0,10}  {1}' -f (Format-Size $f.Length), $f.FullName.Substring($AppDir.Length + 1)))
    }
    $text = $report -join [Environment]::NewLine
    Set-Content -LiteralPath (Join-Path $Dist 'size-report.txt') -Value $text -Encoding ASCII
    Write-Host $text
}

# ------------------------------------------------------------------------------ 1. version
if (-not $Python) { $Python = Join-Path $Repo '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) {
    throw ('Build venv not found at {0}. Run: uv sync --locked --extra cuda --group packaging' -f $Python)
}
$script:Version = (& $Python -c 'import shoutabout; print(shoutabout.__version__)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $script:Version) { throw 'Could not read the shoutabout version.' }
$numeric = @(($script:Version -split '[^0-9.]')[0].Split('.') | Where-Object { $_ -ne '' })
while ($numeric.Count -lt 4) { $numeric += '0' }
$VersionNumeric = ($numeric[0..3] -join '.')
Write-Step ('ShoutAbout {0} (file version {1})' -f $script:Version, $VersionNumeric)
if ($env:GITHUB_OUTPUT) { Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding ASCII -Value ('version={0}' -f $script:Version) }

# ------------------------------------------------------------------------------ 2. assets
Write-Step 'Brand assets'
Invoke-Checked $Python @((Join-Path $Packaging 'make_assets.py'), '--out', $Assets)

# ------------------------------------------------------------------------------ 3. PyInstaller
Write-Step 'PyInstaller (onedir, windowed)'
if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force }
$env:SHOUTABOUT_ASSETS = $Assets
$started = Get-Date
Invoke-Checked $Python @('-m', 'PyInstaller', (Join-Path $Packaging 'shoutabout.spec'),
    '--noconfirm', '--clean', '--log-level', 'WARN',
    '--distpath', $Dist, '--workpath', (Join-Path $Work 'pyinstaller'))
Write-Host ('PyInstaller took {0:N0} s' -f ((Get-Date) - $started).TotalSeconds)
if (-not (Test-Path -LiteralPath (Join-Path $AppDir 'ShoutAbout.exe'))) { throw 'ShoutAbout.exe was not built.' }
# Licensing gate: no forbidden file (an ASIO PortAudio build, PyAV or any FFmpeg DLL such as
# avcodec*, avformat*, x264, x265) may be in the bundle.
Invoke-Checked $Python @((Join-Path $Packaging 'bundle_rules.py'), 'check', $AppDir)

# ------------------------------------------------------------------------------ 4. sizes
Write-Step 'Size report'
Write-SizeReport

# ------------------------------------------------------------------------------ 5. installer
if ($SkipInstaller) { Write-Host 'Skipping the installer (-SkipInstaller).'; exit 0 }
$isccExe = Find-Iscc
if (-not $isccExe) {
    $msg = 'Inno Setup 6 (ISCC.exe) not found: install it from https://jrsoftware.org/isinfo.php or pass -Iscc.'
    if ($RequireInstaller) { throw $msg }
    Write-Warning $msg
    exit 0
}
Write-Step ('Inno Setup: {0}' -f $isccExe)

$isccArgs = New-Object System.Collections.Generic.List[string]
foreach ($a in @(
        ('/DAppVersion={0}' -f $script:Version),
        ('/DAppVersionNumeric={0}' -f $VersionNumeric),
        ('/DSourceDir={0}' -f $AppDir),
        ('/DAssetsDir={0}' -f $Assets),
        ('/DOutputDir={0}' -f $Dist),
        '/Qp')) { $isccArgs.Add($a) }

$importedThumb = ''
$thumb = $env:SHOUTABOUT_SIGN_THUMBPRINT
$pfxFile = ''
try {
    if (-not $thumb -and $env:SHOUTABOUT_SIGN_PFX_BASE64) {
        $pfxFile = Join-Path ([IO.Path]::GetTempPath()) ('shoutabout-sign-{0}.pfx' -f [guid]::NewGuid())
        [IO.File]::WriteAllBytes($pfxFile, [Convert]::FromBase64String($env:SHOUTABOUT_SIGN_PFX_BASE64))
        $secure = ConvertTo-SecureString -String ([string]$env:SHOUTABOUT_SIGN_PFX_PASSWORD) -AsPlainText -Force
        $cert = Import-PfxCertificate -FilePath $pfxFile -CertStoreLocation 'Cert:\CurrentUser\My' -Password $secure
        $thumb = $cert.Thumbprint
        $importedThumb = $thumb
    }
    if ($thumb) {
        $signtool = Find-SignTool
        if (-not $signtool) { throw 'Code signing is configured but signtool.exe (Windows SDK) was not found.' }
        $ts = $env:SHOUTABOUT_SIGN_TIMESTAMP_URL
        if (-not $ts) { $ts = 'http://timestamp.digicert.com' }
        # Inno Setup replaces $f with the quoted file name and $q with a quote.
        $cmd = ('$q{0}$q sign /sha1 {1} /fd sha256 /tr {2} /td sha256 /d $qShoutAbout$q $f' -f $signtool, $thumb, $ts)
        $isccArgs.Add('/DSign')
        $isccArgs.Add(('/Sshoutabout={0}' -f $cmd))
        Write-Host ('Signing with certificate {0}' -f $thumb)
    } else {
        Write-Host 'No code-signing certificate configured: building an UNSIGNED installer.'
    }
    $isccArgs.Add((Join-Path $Packaging 'installer.iss'))
    $started = Get-Date
    Invoke-Checked $isccExe $isccArgs.ToArray()
    Write-Host ('Inno Setup took {0:N0} s' -f ((Get-Date) - $started).TotalSeconds)
} finally {
    if ($pfxFile -and (Test-Path -LiteralPath $pfxFile)) { Remove-Item -LiteralPath $pfxFile -Force }
    if ($importedThumb) {
        Remove-Item -LiteralPath ('Cert:\CurrentUser\My\{0}' -f $importedThumb) -ErrorAction SilentlyContinue
    }
}

$setup = Join-Path $Dist ('ShoutAbout-Setup-{0}.exe' -f $script:Version)
if (-not (Test-Path -LiteralPath $setup)) { throw ('{0} was not created.' -f $setup) }
$setupSize = (Get-Item -LiteralPath $setup).Length
$line = ('Installer: {0} ({1})' -f $setup, (Format-Size $setupSize))
Add-Content -LiteralPath (Join-Path $Dist 'size-report.txt') -Value ('', $line) -Encoding ASCII
Write-Step $line
if ($env:GITHUB_OUTPUT) {
    Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding ASCII -Value @(
        ('installer={0}' -f $setup),
        ('installer_name={0}' -f (Split-Path -Leaf $setup)),
        ('installer_bytes={0}' -f $setupSize))
}
