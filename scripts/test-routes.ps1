<#
.SYNOPSIS
    End-to-end network tests for the hub: LAN route and Cloudflare Tunnel + Access route
    (SETUP.md step 7). Prints PASS/FAIL per check; exit code 1 if anything failed.

.DESCRIPTION
    Uses curl.exe (built into Windows 10/11) and uploads with target=phone, so nothing is pasted
    on the PC. The hub token is read from %APPDATA%\shoutabout\hub_token.txt and is never
    printed; secrets are passed to curl through a temporary header file (not the command line),
    which is deleted afterwards.

    LAN (from this PC to its own LAN address; the Windows firewall is only exercised when the
    iPhone connects, so also test from the phone):
      1. GET  <lan>/health                         -> 200 {"ok": true}
      2. POST <lan>/transcribe without a token     -> 401
      3. POST <lan>/transcribe with a wrong token  -> 401
      4. POST <lan>/transcribe with the token      -> 200, route "lan"
    Public (Cloudflare Tunnel + Access), unless -SkipPublic:
      5. GET  <public>/health without CF headers   -> blocked by Access (401/403, or 302 to
                                                      *.cloudflareaccess.com); 200 = NOT protected
      6. POST <public>/transcribe, CF service-token headers, no bearer -> 401 from the hub
      7. POST <public>/transcribe, CF headers + bearer                 -> 200, route "tunnel"
    The Access service token comes from $env:SHOUTABOUT_CF_CLIENT_ID /
    $env:SHOUTABOUT_CF_CLIENT_SECRET, or is asked for (the secret with hidden input).

    Each failed auth attempt counts toward the hub's lockout ([server] max_auth_failures per
    10 minutes); a 429 means wait 10 minutes.

.PARAMETER LanUrl
    Base LAN URL, e.g. http://192.0.2.10:8765. Default: lan_url from "python -m shoutabout info --json".

.PARAMETER PublicUrl
    Base public URL. Default: https://<public_hostname> from install-state.json.

.PARAMETER AudioFile
    Audio clip to upload. Default: test-audio\vocab-01.m4a (make it with scripts\make_test_audio.ps1).

.PARAMETER SkipPublic
    Only run the LAN tests.

.PARAMETER TimeoutSec
    Per-request timeout in seconds. Default 120.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1 -SkipPublic -LanUrl http://192.0.2.10:8765
#>
[CmdletBinding()]
param(
    [string]$LanUrl = '',
    [string]$PublicUrl = '',
    [string]$AudioFile = '',
    [switch]$SkipPublic,
    [ValidateRange(5, 600)][int]$TimeoutSec = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Results = New-Object System.Collections.Generic.List[object]
$script:Curl = ''

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

function Get-ConfigHome {
    if ($env:SHOUTABOUT_HOME) { return [Environment]::ExpandEnvironmentVariables($env:SHOUTABOUT_HOME) }
    return (Join-Path $env:APPDATA 'shoutabout')
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

function ConvertFrom-JsonSafe([string]$Text) {
    if (-not $Text) { return $null }
    try { return ($Text | ConvertFrom-Json) } catch { return $null }
}

function Invoke-Curl {
    # One HTTP request via curl.exe. Headers go through a temp file so secrets stay off the
    # command line. Never follows redirects (a 302 to Access must stay visible).
    param([string]$Url, [string[]]$Headers = @(), [string]$UploadFile = '')
    $bodyFile = [IO.Path]::GetTempFileName()
    $hdrFile = $null
    try {
        $curlArgs = @('-s', '-S', '-o', $bodyFile, '-w', '%{http_code} %{time_total} %{redirect_url}', '--max-time', [string]$TimeoutSec)
        if (@($Headers).Count -gt 0) {
            $hdrFile = [IO.Path]::GetTempFileName()
            [IO.File]::WriteAllText($hdrFile, ((@($Headers) -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding $false))
            $curlArgs += @('-H', ('@' + $hdrFile))
        }
        if ($UploadFile) { $curlArgs += @('-F', ('file=@' + $UploadFile), '-F', 'target=phone') }
        $curlArgs += $Url
        $r = Invoke-ExeCapture -FilePath $script:Curl -Arguments $curlArgs
        $w = ((@($r.StdOut) -join ' ').Trim() -split '\s+', 3)
        $status = 0
        [void][int]::TryParse([string]$w[0], [ref]$status)
        $seconds = 0.0
        if (@($w).Count -ge 2) { [void][double]::TryParse([string]$w[1], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$seconds) }
        $location = if (@($w).Count -ge 3) { [string]$w[2] } else { '' }
        $body = ''
        if (Test-Path -LiteralPath $bodyFile) { $body = [IO.File]::ReadAllText($bodyFile) }
        return [pscustomobject]@{
            Status    = $status
            Seconds   = $seconds
            Location  = $location
            Body      = $body
            Json      = (ConvertFrom-JsonSafe $body)
            CurlExit  = $r.ExitCode
            CurlError = ((@($r.StdErr) -join ' ').Trim())
        }
    } finally {
        if ($hdrFile -and (Test-Path -LiteralPath $hdrFile)) { Remove-Item -LiteralPath $hdrFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $bodyFile) { Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue }
    }
}

function Get-Snippet($Response) {
    if ($Response.CurlExit -ne 0) { return ('curl error {0}: {1}' -f $Response.CurlExit, $Response.CurlError) }
    $b = ([string]$Response.Body -replace '\s+', ' ').Trim()
    if ($b.Length -gt 160) { $b = $b.Substring(0, 157) + '...' }
    $s = 'HTTP {0}' -f $Response.Status
    if ($Response.Location) { $s += (' -> {0}' -f $Response.Location) }
    if ($b) { $s += (' {0}' -f $b) }
    return $s
}

function Add-Result([string]$Name, [string]$Status, [string]$Expected, [string]$Actual, [string]$Hint = '') {
    $script:Results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Expected = $Expected; Actual = $Actual })
    $color = switch ($Status) { 'PASS' { 'Green' } 'FAIL' { 'Red' } default { 'DarkGray' } }
    Write-Host ('[{0}] {1}' -f $Status, $Name) -ForegroundColor $color
    if ($Status -ne 'SKIP') {
        Write-Host ('       expected: {0}' -f $Expected)
        Write-Host ('       actual  : {0}' -f $Actual)
    } elseif ($Actual) {
        Write-Host ('       {0}' -f $Actual) -ForegroundColor DarkGray
    }
    if ($Hint -and $Status -eq 'FAIL') { Write-Host ('       hint    : {0}' -f $Hint) -ForegroundColor Yellow }
}

function Get-StatusHint([int]$Status) {
    switch ($Status) {
        0 { return 'No HTTP answer: is the hub running (tray icon), is the URL right, and does DNS resolve?' }
        403 { return 'Source address not allowed, or (tunnel) the Access JWT was rejected: check [access] team_domain/aud.' }
        429 { return 'Rate limit or auth-failure lockout ([server] max_auth_failures per 10 min); wait and retry.' }
        503 { return 'The speech model is still loading or failed to load: run python -m shoutabout doctor.' }
        default { return '' }
    }
}

function Write-Timings($Response) {
    $t = Get-Prop $Response.Json 'timings_ms' $null
    $parts = New-Object System.Collections.Generic.List[string]
    if ($null -ne $t) { foreach ($p in $t.PSObject.Properties) { $parts.Add(('{0}={1}' -f $p.Name, $p.Value)) } }
    Write-Host ('       timings_ms: {0}   (round trip {1:N0} ms)' -f ($parts -join ' '), ($Response.Seconds * 1000))
    $text = [string](Get-Prop $Response.Json 'text' '')
    if ($text.Length -gt 120) { $text = $text.Substring(0, 117) + '...' }
    Write-Host ('       text      : {0}   (cleanup: {1})' -f $text, (Get-Prop $Response.Json 'cleanup' '?'))
}

function Test-Transcribe200([string]$Name, $Response, [string]$Route) {
    $gotRoute = [string](Get-Prop $Response.Json 'route' '')
    if ($Response.Status -eq 200 -and $gotRoute -eq $Route) {
        Add-Result $Name 'PASS' ('200, route "{0}"' -f $Route) ('200, route "{0}"' -f $gotRoute)
        Write-Timings $Response
    } else {
        Add-Result $Name 'FAIL' ('200, route "{0}"' -f $Route) (Get-Snippet $Response) (Get-StatusHint $Response.Status)
    }
}

function Invoke-RouteTests {
    $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath.TrimEnd('\')
    $configHome = Get-ConfigHome
    $py = Join-Path $repoRoot '.venv\Scripts\python.exe'

    $curlCmd = Get-Command curl.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $curlCmd) { throw 'curl.exe not found (it ships with Windows 10 1803+ in C:\Windows\System32).' }
    $script:Curl = $curlCmd.Source

    $tokenFile = Join-Path $configHome 'hub_token.txt'
    if (-not (Test-Path -LiteralPath $tokenFile)) { throw ('{0} not found; run setup.ps1 (or python -m shoutabout init) first.' -f $tokenFile) }
    $token = ([IO.File]::ReadAllText($tokenFile)).Trim()
    if ($token.Length -lt 32) { throw ('The hub token in {0} looks invalid.' -f $tokenFile) }

    $info = $null
    if (Test-Path -LiteralPath $py) {
        $r = Invoke-ExeCapture -FilePath $py -Arguments @('-m', 'shoutabout', 'info', '--json')
        if ($r.ExitCode -eq 0) { $info = ConvertFrom-JsonSafe ((@($r.StdOut) -join "`n").Trim()) }
    }
    if (-not $LanUrl) { $LanUrl = [string](Get-Prop $info 'lan_url' '') }
    if (-not $LanUrl) { throw 'Could not determine the LAN URL; pass -LanUrl http://<pc-lan-ip>:<port>' }
    $LanUrl = $LanUrl.TrimEnd('/')

    if (-not $AudioFile) { $AudioFile = Join-Path $repoRoot 'test-audio\vocab-01.m4a' }
    elseif (-not [IO.Path]::IsPathRooted($AudioFile)) { $AudioFile = Join-Path (Get-Location).ProviderPath $AudioFile }
    if (-not (Test-Path -LiteralPath $AudioFile)) {
        throw ('{0} not found. Create test clips first: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_test_audio.ps1' -f $AudioFile)
    }
    $AudioFile = [IO.Path]::GetFullPath($AudioFile)

    $runPublic = -not $SkipPublic
    if ($runPublic -and -not $PublicUrl) {
        $statePath = Join-Path $configHome 'install-state.json'
        $fqdn = ''
        if (Test-Path -LiteralPath $statePath) {
            try { $fqdn = [string](Get-Prop ([IO.File]::ReadAllText($statePath) | ConvertFrom-Json) 'public_hostname' '') } catch { }
        }
        if (-not $fqdn) { $fqdn = [string](Get-Prop $info 'public_hostname' '') }
        if ($fqdn) { $PublicUrl = 'https://' + $fqdn } else { $runPublic = $false }
    }
    if ($PublicUrl) { $PublicUrl = $PublicUrl.TrimEnd('/') }

    Write-Host 'ShoutAbout route tests' -ForegroundColor White
    Write-Host ('  LAN URL    : {0}' -f $LanUrl)
    Write-Host ('  Public URL : {0}' -f $(if ($runPublic) { $PublicUrl } else { '(skipped)' }))
    Write-Host ('  Audio      : {0}' -f $AudioFile)
    if ($null -ne $info -and -not [bool](Get-Prop $info 'access_enabled' $true)) {
        Write-Host '  WARN       : [access] is not configured, so the hub does not check the Access JWT on the tunnel route.' -ForegroundColor Yellow
    }
    Write-Host ''

    $bearer = 'Authorization: Bearer ' + $token
    $wrong = 'Authorization: Bearer wrong-' + [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')

    # ------------------------------------------------------------------ LAN
    Write-Host 'LAN route' -ForegroundColor Cyan
    $r = Invoke-Curl -Url ($LanUrl + '/health')
    if ($r.Status -eq 200 -and [bool](Get-Prop $r.Json 'ok' $false)) { Add-Result 'LAN /health' 'PASS' '200 {"ok": true}' (Get-Snippet $r) }
    else { Add-Result 'LAN /health' 'FAIL' '200 {"ok": true}' (Get-Snippet $r) (Get-StatusHint $r.Status) }

    $r = Invoke-Curl -Url ($LanUrl + '/transcribe') -UploadFile $AudioFile
    if ($r.Status -eq 401) { Add-Result 'LAN /transcribe, no token' 'PASS' '401' (Get-Snippet $r) }
    else { Add-Result 'LAN /transcribe, no token' 'FAIL' '401' (Get-Snippet $r) (Get-StatusHint $r.Status) }

    $r = Invoke-Curl -Url ($LanUrl + '/transcribe') -Headers @($wrong) -UploadFile $AudioFile
    if ($r.Status -eq 401) { Add-Result 'LAN /transcribe, wrong token' 'PASS' '401' (Get-Snippet $r) }
    else { Add-Result 'LAN /transcribe, wrong token' 'FAIL' '401' (Get-Snippet $r) (Get-StatusHint $r.Status) }

    $r = Invoke-Curl -Url ($LanUrl + '/transcribe') -Headers @($bearer) -UploadFile $AudioFile
    Test-Transcribe200 'LAN /transcribe, hub token' $r 'lan'

    # ------------------------------------------------------------------ public
    Write-Host ''
    Write-Host 'Public route (Cloudflare Tunnel + Access)' -ForegroundColor Cyan
    if (-not $runPublic) {
        foreach ($n in @('Public /health without Access headers', 'Public /transcribe, Access headers, no bearer', 'Public /transcribe, Access headers + bearer')) {
            Add-Result $n 'SKIP' '' '-SkipPublic, or no public_hostname in install-state.json (pass -PublicUrl)'
        }
    } else {
        $r = Invoke-Curl -Url ($PublicUrl + '/health')
        $blocked = ($r.Status -eq 401 -or $r.Status -eq 403 -or
            (@(301, 302, 303, 307, 308) -contains $r.Status -and $r.Location -match '(?i)^https://[^/]+\.cloudflareaccess\.com/'))
        if ($r.CurlExit -eq 0 -and $blocked) {
            Add-Result 'Public /health without Access headers' 'PASS' 'blocked by Access (401/403 or 302 to *.cloudflareaccess.com)' (Get-Snippet $r)
        } elseif ($r.Status -eq 200) {
            Add-Result 'Public /health without Access headers' 'FAIL' 'blocked by Access' (Get-Snippet $r) 'The hostname is NOT protected by Cloudflare Access: create the Access application (SETUP.md step 5b).'
        } else {
            Add-Result 'Public /health without Access headers' 'FAIL' 'blocked by Access' (Get-Snippet $r) 'Check the tunnel route / DNS for the hostname (python -m shoutabout doctor).'
        }

        $cfId = [string]$env:SHOUTABOUT_CF_CLIENT_ID
        $cfSecret = [string]$env:SHOUTABOUT_CF_CLIENT_SECRET
        if (-not $cfId) { $cfId = (Read-Host 'Cloudflare Access service token Client ID (Enter to skip)').Trim() }
        if ($cfId -and -not $cfSecret) {
            $sec = Read-Host 'Client Secret (hidden)' -AsSecureString
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
            try { $cfSecret = ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        }
        if (-not $cfId -or -not $cfSecret) {
            foreach ($n in @('Public /transcribe, Access headers, no bearer', 'Public /transcribe, Access headers + bearer')) {
                Add-Result $n 'SKIP' '' 'No Access service token given (set SHOUTABOUT_CF_CLIENT_ID / SHOUTABOUT_CF_CLIENT_SECRET).'
            }
        } else {
            $cf = @(('CF-Access-Client-Id: ' + $cfId), ('CF-Access-Client-Secret: ' + $cfSecret))
            $r = Invoke-Curl -Url ($PublicUrl + '/transcribe') -Headers $cf -UploadFile $AudioFile
            $fromHub = ($null -ne $r.Json -and $null -ne (Get-Prop $r.Json 'detail' $null))
            if ($r.Status -eq 401 -and $fromHub) {
                Add-Result 'Public /transcribe, Access headers, no bearer' 'PASS' '401 from the hub' (Get-Snippet $r)
            } elseif ($r.Status -eq 401 -or $r.Status -eq 302 -or ($r.Status -eq 403 -and -not $fromHub)) {
                Add-Result 'Public /transcribe, Access headers, no bearer' 'FAIL' '401 from the hub' (Get-Snippet $r) 'Cloudflare Access rejected the service token: check Client ID/Secret and the Service Auth policy on the application.'
            } else {
                Add-Result 'Public /transcribe, Access headers, no bearer' 'FAIL' '401 from the hub' (Get-Snippet $r) (Get-StatusHint $r.Status)
            }
            $r = Invoke-Curl -Url ($PublicUrl + '/transcribe') -Headers (@($cf) + @($bearer)) -UploadFile $AudioFile
            Test-Transcribe200 'Public /transcribe, Access headers + bearer' $r 'tunnel'
        }
        $cfSecret = $null
    }
    $token = $null

    # ------------------------------------------------------------------ summary
    $pass = @($script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
    $fail = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
    $skip = @($script:Results | Where-Object { $_.Status -eq 'SKIP' }).Count
    Write-Host ''
    $color = if ($fail -gt 0) { 'Red' } else { 'Green' }
    Write-Host ('{0} passed, {1} failed, {2} skipped' -f $pass, $fail, $skip) -ForegroundColor $color
    if ($fail -gt 0) { return 1 }
    return 0
}

if ($MyInvocation.InvocationName -ne '.') {
    $code = 1
    try {
        $code = Invoke-RouteTests
    } catch {
        Write-Host ('test-routes: {0}' -f $_.Exception.Message) -ForegroundColor Red
        $code = 1
    }
    exit $code
}
