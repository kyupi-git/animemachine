[CmdletBinding()]
param([string]$Directory = '.')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($Directory)
$example = Join-Path $root '.env.example'
$environment = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $example -PathType Leaf)) { throw ".env.example was not found in $root" }
$composeFile = Join-Path $root 'compose.yaml'
if (Test-Path -LiteralPath $composeFile -PathType Leaf) {
    $composeText = [IO.File]::ReadAllText($composeFile)
    if ($composeText -match '(?m)^\s+(?:qbittorrent|ani-rss):') {
        $rawVersion = (& docker compose version --short 2>$null)
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($rawVersion)) { throw 'Docker Compose 2.20.3 or newer is required.' }
        $match = [regex]::Match([string]$rawVersion, '(\d+)\.(\d+)\.(\d+)')
        if (-not $match.Success) { throw "Unable to determine Docker Compose version: $rawVersion" }
        $composeVersion = [version]$match.Value
        if ($composeVersion -lt [version]'2.20.3') { throw "Docker Compose 2.20.3 or newer is required (found $composeVersion)." }
    }
}
foreach ($relative in @('config\qbittorrent','config\ani-rss','config\incomplete','data','imports','torrents','library','external\read-only','external\ani-rss')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root $relative) | Out-Null
}
if (-not (Test-Path -LiteralPath $environment)) { Copy-Item -LiteralPath $example -Destination $environment }

function Protect-AnmCredentialFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    try {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls.exe $Path /inheritance:r /grant:r "*${sid}:F" '*S-1-5-18:F' /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls exited with code $LASTEXITCODE" }
    } catch {
        Write-Warning "Unable to restrict credential file permissions: $Path"
    }
}

function New-HexSecret([string]$Prefix) {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $Prefix + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}
function New-QbtKey {
    $bytes = New-Object byte[] 21
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return 'qbt_' + [Convert]::ToBase64String($bytes).Replace('+','A').Replace('/','B').TrimEnd('=')
}

$text = [IO.File]::ReadAllText($environment)
$generated = @{}
foreach ($entry in @(
    @{Name='ANM_QBT_API_KEY';Value=(New-QbtKey)},
    @{Name='ANM_QBT_WEB_PASSWORD';Value=(New-HexSecret 'qbtweb_')},
    @{Name='ANM_ANI_RSS_API_KEY';Value=(New-HexSecret 'ani_')},
    @{Name='ANM_ADMIN_PASSWORD';Value=(New-HexSecret 'anm_')}
)) {
    $pattern = '(?m)^' + [regex]::Escape($entry.Name) + '=(.*)$'
    $match = [regex]::Match($text, $pattern)
    if (-not $match.Success -or [string]::IsNullOrWhiteSpace($match.Groups[1].Value)) {
        $line = $entry.Name + '=' + $entry.Value
        $text = if ($match.Success) { [regex]::Replace($text, $pattern, $line, 1) } else { $text.TrimEnd() + [Environment]::NewLine + $line + [Environment]::NewLine }
        $generated[$entry.Name] = $true
    }
}
[IO.File]::WriteAllText($environment, $text, [Text.UTF8Encoding]::new($false))
Protect-AnmCredentialFile $environment
$values = @{}
foreach ($line in $text -split '\r?\n') {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
}
$admin = if ($values['ANM_ADMIN_USERNAME']) { $values['ANM_ADMIN_USERNAME'] } else { 'admin' }
$qbtUser = if ($values['ANM_QBT_WEB_USERNAME']) { $values['ANM_QBT_WEB_USERNAME'] } else { 'admin' }
if ($generated.Count -gt 0) {
    Write-Host ''
    Write-Host 'AnimeMachine initial access'
    if ($generated.ContainsKey('ANM_ADMIN_PASSWORD')) {
        $webPort = if ($values['ANM_WEB_PORT']) { $values['ANM_WEB_PORT'] } else { '8787' }
        Write-Host ("  AnimeMachine  url=http://localhost:{0}  user={1}  password={2}" -f $webPort, $admin, $values['ANM_ADMIN_PASSWORD'])
    }
    if ($generated.ContainsKey('ANM_QBT_WEB_PASSWORD')) {
        Write-Host ("  qBittorrent  user={0}  password={1}" -f $qbtUser, $values['ANM_QBT_WEB_PASSWORD'])
    }
    if ($generated.ContainsKey('ANM_ANI_RSS_API_KEY')) {
        Write-Host ("  Ani-RSS      API key={0}" -f $values['ANM_ANI_RSS_API_KEY'])
    }
    Write-Host "Private values are stored in $environment"
} else {
    Write-Host "Existing credentials preserved in $environment"
}
