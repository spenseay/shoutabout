<#
.SYNOPSIS
Publishes ShoutAbout downloads (Windows installer and/or Mac disk image) as a GitHub release
on the public downloads repo, under stable file names the website links to.

.DESCRIPTION
The source repo stays private. Downloads are published on a separate PUBLIC repo that holds
only a README, release notes and binaries (see docs/launch/hosting.md). The website links to
https://github.com/<Repo>/releases/latest/download/<name>, with names that never change:

    ShoutAbout-Setup.exe   (Windows)
    ShoutAbout.dmg         (Mac)

So the LATEST release must always carry both files (once both exist). This script:

  1. checks each file you pass (.exe or .dmg; any local name, it is uploaded under the
     stable name), its size, SHA-256 and signature (Windows: Authenticode, unsigned is
     reported, not refused);
  2. checks gh is logged in, the target repo exists and is PUBLIC, and the tag is new;
  3. carries forward the other platform's file from the current latest release, so a
     Windows-only release doesn't break the Mac link (and the other way round);
  4. writes SHA256SUMS.txt and release notes (or uses -NotesFile);
  5. runs: gh release create v<Version> <files> SHA256SUMS.txt --repo <Repo> --latest;
  6. checks the uploaded sizes, and prints (or, with -UpdateSiteConfig, writes) the sizes for
     site/downloads.json. It never flips the "live" flags: that stays a deliberate step.

It never creates repos, never pushes tags to the source repo and never deletes anything but
its own temporary files. A release that already exists is left alone (the script stops).
With -DryRun it only checks the files and prints what it would do; apart from read-only
lookups of the current latest release it doesn't call gh.

Requirements: Windows PowerShell 5.1 or PowerShell 7, and the GitHub CLI (gh) logged in with
an account that can create releases on -Repo (gh auth login).

.PARAMETER InstallerPath
One or more files to publish: the Windows installer (.exe, normally
packaging\dist\ShoutAbout-Setup-<version>.exe) and/or the Mac disk image (.dmg, signed and
notarized, for example from the Codemagic build).

.PARAMETER Version
The version being released, for example 1.2.0. The tag is v<Version>. It appears only on
GitHub's release page, never in the file names or on the website.

.PARAMETER Repo
The public downloads repo as OWNER/NAME. Defaults to the SHOUTABOUT_RELEASES_REPO
environment variable.

.PARAMETER NotesFile
Optional Markdown file with the release notes. Without it, short standard notes are written.

.PARAMETER Draft
Create the release as a draft. Draft downloads aren't public, and a draft is never "latest":
publish it on GitHub afterwards.

.PARAMETER UpdateSiteConfig
Also write size_bytes for each published platform into site/downloads.json (in this
checkout). Review and commit that change yourself.

.PARAMETER DryRun
Check the files and print the gh command and the downloads.json values without creating
anything.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release\publish-github-release.ps1 -InstallerPath packaging\dist\ShoutAbout-Setup-1.2.0.exe -Version 1.2.0 -Repo OWNER/shoutabout -DryRun

Checks the installer and shows what would be published (and which Mac file would be carried
forward from the current latest release).

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release\publish-github-release.ps1 -InstallerPath packaging\dist\ShoutAbout-Setup-1.2.0.exe,C:\Downloads\ShoutAbout.dmg -Version 1.2.0 -Repo OWNER/shoutabout -UpdateSiteConfig

Publishes release v1.2.0 with both downloads and SHA256SUMS.txt, then writes their sizes into
site/downloads.json.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Repo = $env:SHOUTABOUT_RELEASES_REPO,
    [string]$NotesFile = '',
    [switch]$Draft,
    [switch]$UpdateSiteConfig,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "powershell -File" passes "a.exe,b.dmg" as one string: split it here.
$InstallerPath = @($InstallerPath | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# Stable names: the website's links never change (docs/launch/hosting.md).
$StableNames = [ordered]@{ '.exe' = 'ShoutAbout-Setup.exe'; '.dmg' = 'ShoutAbout.dmg' }
$Platforms = @{ 'ShoutAbout-Setup.exe' = 'windows'; 'ShoutAbout.dmg' = 'mac' }

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Stop-WithError([string]$Text) { Write-Host "ERROR: $Text" -ForegroundColor Red; exit 1 }

function Invoke-Gh {
    # Runs gh with the given arguments; returns @{ Code; Out } (stdout and stderr merged).
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # gh writes progress to stderr; judge by exit code
    try {
        $out = & gh.exe @Arguments 2>&1 | ForEach-Object { "$_" }
        return @{ Code = $LASTEXITCODE; Out = ($out -join "`n") }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $Text = $Text -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# ------------------------------------------------------------------ check the inputs

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Stop-WithError "Version must look like 1.2.0 (got '$Version')."
}
if (-not $Repo) {
    Stop-WithError 'Pass -Repo OWNER/NAME or set SHOUTABOUT_RELEASES_REPO.'
}
if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    Stop-WithError "Repo must be OWNER/NAME (got '$Repo')."
}
if ($NotesFile -and -not (Test-Path -LiteralPath $NotesFile -PathType Leaf)) {
    Stop-WithError "Notes file not found: $NotesFile"
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("shoutabout-release-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null

# name -> @{ Path; Size; Sha; Source }
$files = [ordered]@{}
foreach ($path in $InstallerPath) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Stop-WithError "File not found: $path" }
    $item = Get-Item -LiteralPath $path
    $ext = $item.Extension.ToLowerInvariant()
    if (-not $StableNames.Contains($ext)) {
        Stop-WithError "$($item.Name): only .exe (Windows installer) and .dmg (Mac disk image) are published."
    }
    $name = $StableNames[$ext]
    if ($files.Contains($name)) { Stop-WithError "Two files would both be published as $name." }

    Write-Step "Checking $($item.Name) (published as $name)"
    $size = [long]$item.Length
    if ($size -ge 2GB) { Stop-WithError 'GitHub release files must be smaller than 2 GiB.' }
    $sha = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "    size     $size bytes (about $([math]::Round($size / 1000000)) MB)"
    Write-Host "    sha256   $sha"
    if ($ext -eq '.exe') {
        $signature = Get-AuthenticodeSignature -LiteralPath $item.FullName
        Write-Host "    signed   $($signature.Status)"
        if ($signature.Status -ne 'Valid') {
            Write-Host '    (Not code-signed: expected until the Microsoft Store version. SmartScreen will ask "More info > Run anyway".)' -ForegroundColor Yellow
        }
    } else {
        Write-Host '    (Signing and notarization of a .dmg can only be checked on a Mac: spctl -a -t open --context context:primary-signature -v ShoutAbout.dmg)' -ForegroundColor Yellow
    }
    # Upload a copy under the stable name (gh names the asset after the file).
    $copy = Join-Path $work $name
    if (-not $DryRun) { Copy-Item -LiteralPath $item.FullName -Destination $copy }
    $files[$name] = @{ Path = $copy; Size = $size; Sha = $sha; Source = 'new' }
}

$tag = "v$Version"
$released = (Get-Date).ToString('yyyy-MM-dd')

# --------------------------------------------- the other platform, from the latest release

$haveGh = [bool](Get-Command gh.exe -ErrorAction SilentlyContinue)
if (-not $haveGh -and -not $DryRun) {
    Stop-WithError 'The GitHub CLI (gh) is not installed: https://cli.github.com'
}
$latestTag = ''
$latestAssets = @()
if ($haveGh) {
    $latest = Invoke-Gh @('release', 'view', '--repo', $Repo, '--json', 'tagName,assets')
    if ($latest.Code -eq 0) {
        $info = $latest.Out | ConvertFrom-Json
        $latestTag = $info.tagName
        $latestAssets = @($info.assets)
    }
}
foreach ($name in $StableNames.Values) {
    if ($files.Contains($name)) { continue }
    $asset = @($latestAssets | Where-Object { $_.name -eq $name }) | Select-Object -First 1
    if (-not $asset) {
        Write-Host ("    No $name in the current latest release" +
            $(if ($latestTag) { " ($latestTag)" } else { '' }) +
            ": its link (releases/latest/download/$name) will 404, so keep $($Platforms[$name]).live false in site/downloads.json.") -ForegroundColor Yellow
        continue
    }
    Write-Step "Carrying $name forward from $latestTag"
    $copy = Join-Path $work $name
    if ($DryRun) {
        Write-Host "    Dry run: would download it with: gh release download $latestTag --repo $Repo --pattern $name"
        $files[$name] = @{ Path = $copy; Size = [long]$asset.size; Sha = '(computed after download)'; Source = $latestTag }
        continue
    }
    $dl = Invoke-Gh @('release', 'download', $latestTag, '--repo', $Repo, '--pattern', $name, '--dir', $work)
    if ($dl.Code -ne 0 -or -not (Test-Path -LiteralPath $copy -PathType Leaf)) {
        Stop-WithError "Couldn't download $name from $latestTag. Nothing was published.`n$($dl.Out)"
    }
    $size = (Get-Item -LiteralPath $copy).Length
    if ($size -ne [long]$asset.size) { Stop-WithError "Downloaded $name is $size bytes, expected $($asset.size)." }
    $sha = (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash.ToLowerInvariant()
    $files[$name] = @{ Path = $copy; Size = [long]$size; Sha = $sha; Source = $latestTag }
}

# ------------------------------------------------------------- notes and checksums

$sums = Join-Path $work 'SHA256SUMS.txt'
$sumLines = foreach ($name in $files.Keys) { "$($files[$name].Sha)  $name" }
Write-Utf8NoBom $sums (($sumLines -join "`n") + "`n")

if ($NotesFile) {
    $notes = (Resolve-Path -LiteralPath $NotesFile).Path
} else {
    $notes = Join-Path $work 'notes.md'
    $lines = @("Private voice dictation. Free. https://shoutabout.fyi", '')
    if ($files.Contains('ShoutAbout-Setup.exe')) {
        $lines += @(
            '**Windows** (10 version 1809 or later, or 11, 64-bit): download **ShoutAbout-Setup.exe**, open it.',
            'Until the Microsoft Store version exists, Windows says "Windows protected your PC": click',
            '**More info**, check the file name, then **Run anyway**. Then **Yes**, **Next**, **Finish**.',
            ''
        )
    }
    if ($files.Contains('ShoutAbout.dmg')) {
        $lines += @(
            '**Mac** (Apple silicon, macOS 14 or later): download **ShoutAbout.dmg**, open it and drag',
            'ShoutAbout into Applications. Signed with a Developer ID and notarized by Apple.',
            ''
        )
    }
    $lines += @('SHA-256 checksums are in SHA256SUMS.txt.', '', 'Help: https://shoutabout.fyi/support')
    Write-Utf8NoBom $notes (($lines -join "`n") + "`n")
}

$ghArgs = @('release', 'create', $tag) + @($files.Keys | ForEach-Object { $files[$_].Path }) + @($sums,
    '--repo', $Repo, '--title', 'ShoutAbout', '--notes-file', $notes)
if ($Draft) { $ghArgs += '--draft' } else { $ghArgs += '--latest' }

# ---------------------------------------------------------------------- publish

if ($DryRun) {
    Write-Step 'Dry run: nothing is created. This would run:'
    Write-Host ('    gh ' + (($ghArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '))
    Write-Step 'Release notes:'
    Get-Content -LiteralPath $notes | ForEach-Object { Write-Host "    $_" }
} else {
    Write-Step 'Checking GitHub'
    $auth = Invoke-Gh @('auth', 'status')
    if ($auth.Code -ne 0) { Stop-WithError "gh isn't logged in. Run: gh auth login`n$($auth.Out)" }

    $view = Invoke-Gh @('repo', 'view', $Repo, '--json', 'visibility,isPrivate,isArchived')
    if ($view.Code -ne 0) {
        Stop-WithError ("Repo $Repo not found or not accessible. Create it first (docs/launch/hosting.md):`n" +
            "    gh repo create $Repo --public --add-readme --description `"ShoutAbout downloads`"`n$($view.Out)")
    }
    $repoInfo = $view.Out | ConvertFrom-Json
    if ($repoInfo.isPrivate -or $repoInfo.visibility -ne 'PUBLIC') {
        Stop-WithError "$Repo is not public. Downloads must come from a public repo."
    }
    if ($repoInfo.isArchived) { Stop-WithError "$Repo is archived." }

    $existing = Invoke-Gh @('release', 'view', $tag, '--repo', $Repo, '--json', 'tagName')
    if ($existing.Code -eq 0) {
        Stop-WithError ("Release $tag already exists on $Repo. Nothing was changed. To replace a file, " +
            "upload it yourself: gh release upload $tag <file> --repo $Repo --clobber")
    }

    $totalMb = [math]::Round((($files.Values | ForEach-Object { $_.Size } | Measure-Object -Sum).Sum) / 1000000)
    Write-Step "Creating release $tag on $Repo (uploading about $totalMb MB, this takes a while)"
    $created = Invoke-Gh $ghArgs
    if ($created.Code -ne 0) { Stop-WithError "gh release create failed:`n$($created.Out)" }
    Write-Host "    $($created.Out)"

    $assets = Invoke-Gh @('release', 'view', $tag, '--repo', $Repo, '--json', 'assets,isDraft,url')
    if ($assets.Code -eq 0) {
        $rel = $assets.Out | ConvertFrom-Json
        foreach ($name in $files.Keys) {
            $asset = @($rel.assets | Where-Object { $_.name -eq $name }) | Select-Object -First 1
            if (-not $asset) { Stop-WithError "The release exists but $name isn't attached. Check $($rel.url)" }
            if ([long]$asset.size -ne $files[$name].Size) {
                Stop-WithError "Uploaded $name is $($asset.size) bytes, the local file $($files[$name].Size). Re-upload with --clobber."
            }
        }
        if ($rel.isDraft) {
            Write-Host ('    Draft: the links work only after you click Publish release on ' + $rel.url) -ForegroundColor Yellow
        }
    }
}

# ------------------------------------------------------------- website values

Write-Step 'Values for site/downloads.json:'
foreach ($name in $files.Keys) {
    $platform = $Platforms[$name]
    Write-Host ("    {0}.size_bytes = {1}   ({2}; https://github.com/{3}/releases/latest/download/{2})" -f
        $platform, $files[$name].Size, $name, $Repo)
}
Write-Host '    The live flags are not changed here. Set windows.live / mac.live to true yourself once the link works.'

if ($UpdateSiteConfig) {
    $siteConfig = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\site\downloads.json'))
    if (-not (Test-Path -LiteralPath $siteConfig -PathType Leaf)) { Stop-WithError "Not found: $siteConfig" }
    if ($DryRun) {
        Write-Step "Dry run: would update size_bytes in $siteConfig"
    } else {
        $config = Get-Content -LiteralPath $siteConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($name in $files.Keys) {
            $block = $config.($Platforms[$name])
            if ($block.PSObject.Properties.Name -contains 'size_bytes') {
                $block.size_bytes = $files[$name].Size
            } else {
                $block | Add-Member -NotePropertyName size_bytes -NotePropertyValue $files[$name].Size
            }
        }
        Write-Utf8NoBom $siteConfig (($config | ConvertTo-Json -Depth 6) + "`n")
        Write-Step "Updated $siteConfig. Review it (git diff), commit, and push: Cloudflare Pages redeploys."
    }
}

Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
if (-not $DryRun -and -not $Draft) {
    Write-Step "Done ($released). Test the links in a browser before announcing them:"
    foreach ($name in $files.Keys) { Write-Host "    https://github.com/$Repo/releases/latest/download/$name" }
}
