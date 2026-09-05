[CmdletBinding()]
param(
    [ValidateRange(0, 65535)][int]$Port = 0,
    [string]$BindAddress = '',
    [switch]$DisableSubmission,
    [switch]$DisableAuthentication,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$packagedApp = Test-Path -LiteralPath (Join-Path $PSScriptRoot 'app\animemachine') -PathType Container
$packagedWheels = Test-Path -LiteralPath (Join-Path $PSScriptRoot 'packages') -PathType Container
$releaseLayout = $packagedApp -or $packagedWheels
if ($releaseLayout) {
    $projectRoot = $PSScriptRoot
} else {
    $projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
}
$appRoot = if ($packagedApp) { Join-Path $projectRoot 'app' } elseif (-not $releaseLayout) { Join-Path $projectRoot 'src' } else { '' }
$env:ANM_INSTALL_ROOT = $projectRoot
$env:ANM_INSTALL_MODE = if ($releaseLayout) { 'portable' } else { 'source' }
$exampleConfig = Join-Path $projectRoot 'config\config.example.json'
$environmentExample = if ($releaseLayout) {
    Join-Path $projectRoot '.env.local.example'
} else {
    Join-Path $projectRoot 'deploy\local\.env.local.example'
}
$environmentFile = if ($releaseLayout) {
    Join-Path $projectRoot '.env.local'
} else {
    Join-Path $projectRoot '.local\.env.local'
}

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

function Import-EnvironmentFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$' -and
            [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($Matches[1], 'Process'))) {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}
function Resolve-AnmPath([string]$Value, [string]$Default) {
    $candidate = if ([string]::IsNullOrWhiteSpace($Value)) { $Default } else { $Value }
    if ([IO.Path]::IsPathRooted($candidate)) { return [IO.Path]::GetFullPath($candidate) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $candidate))
}
function Get-AnmProbeHost([string]$HostValue) {
    $value = $HostValue.Trim()
    if ([string]::IsNullOrWhiteSpace($value) -or $value -eq '0.0.0.0' -or $value -eq '*') { return '127.0.0.1' }
    if ($value -eq '::') { return '[::1]' }
    if ($value.StartsWith('[') -and $value.EndsWith(']')) { return $value }
    if ($value.Contains(':')) { return "[$value]" }
    return $value
}
function Get-AnmListener([int]$ListenPort) {
    $pattern = "^\s*TCP\s+\S+:$ListenPort\s+\S+\s+LISTENING\s+(\d+)\s*$"
    @(netstat -ano -p tcp | ForEach-Object {
        if ($_ -match $pattern) { [int]$Matches[1] }
    } | Sort-Object -Unique)
}
function Get-SystemPython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { return [pscustomobject]@{ Exe = $launcher.Source; Prefix = @('-3') } }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\python.exe') {
        return [pscustomobject]@{ Exe = $command.Source; Prefix = @() }
    }
    throw 'Python 3.11 or newer was not found. Install Python, or use the Windows Release package.'
}

if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    $parent = Split-Path -Parent $environmentFile
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $environmentExample -PathType Leaf) {
        Copy-Item -LiteralPath $environmentExample -Destination $environmentFile
    } else {
        New-Item -ItemType File -Path $environmentFile | Out-Null
    }
}
Protect-AnmCredentialFile $environmentFile
Import-EnvironmentFile $environmentFile
$env:ANM_ENV_FILE = $environmentFile

$env:ANM_AUTH_ENABLED = if ($DisableAuthentication) { 'false' } elseif ($env:ANM_AUTH_ENABLED) { $env:ANM_AUTH_ENABLED } else { '' }
$env:ANM_SUBMISSION_ENABLED = if ($DisableSubmission) { 'false' } elseif ($env:ANM_SUBMISSION_ENABLED) { $env:ANM_SUBMISSION_ENABLED } else { 'true' }
$environmentPort = 0
$validPort = [int]::TryParse($env:ANM_WEB_PORT, [ref]$environmentPort) -and $environmentPort -ge 1 -and $environmentPort -le 65535
$Port = if ($Port -gt 0) { $Port } elseif ($validPort) { $environmentPort } else { 8787 }
$BindAddress = if ($BindAddress) { $BindAddress } elseif ($env:ANM_BIND_ADDRESS) { $env:ANM_BIND_ADDRESS } else { '0.0.0.0' }

$config = Resolve-AnmPath $env:ANM_CONFIG_PATH (Join-Path $projectRoot 'config.json')
$stateRoot = Resolve-AnmPath $env:ANM_STATE_DIR (Join-Path $projectRoot $(if ($releaseLayout) { 'data\state' } else { '.local\state' }))
$catalog = Resolve-AnmPath $env:ANM_CATALOG_DB (Join-Path $stateRoot 'catalog\anime-catalog.sqlite3')
$runtime = Resolve-AnmPath $env:ANM_RUNTIME_CATALOG_DB (Join-Path $stateRoot 'catalog\runtime.sqlite3')
$archive = Resolve-AnmPath $env:ANM_ARCHIVE_DIR (Join-Path $stateRoot 'metadata\archive')
$cache = Resolve-AnmPath $env:ANM_METADATA_CACHE_DIR (Join-Path $stateRoot 'metadata\cache')

if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $exampleConfig -PathType Leaf)) { throw "Default configuration is missing: $exampleConfig" }
    $configParent = Split-Path -Parent $config
    if ($configParent) { New-Item -ItemType Directory -Force -Path $configParent | Out-Null }
    Copy-Item -LiteralPath $exampleConfig -Destination $config
}
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null

$pythonPrefix = @()
$embeddedPython = Join-Path $projectRoot 'runtime\python.exe'
if (Test-Path -LiteralPath $embeddedPython -PathType Leaf) {
    $python = $embeddedPython
} elseif (-not $releaseLayout) {
    $venvPython = Join-Path $projectRoot '.local\venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $system = Get-SystemPython
        $python = $system.Exe
        $pythonPrefix = @($system.Prefix)
        & $python @pythonPrefix -m venv (Join-Path $projectRoot '.local\venv')
        if ($LASTEXITCODE) { throw 'Unable to create the local Python environment.' }
        $pythonPrefix = @()
    }
    $python = $venvPython
    & $python -c "import animemachine,httpx,PIL,certifi,truststore" 2>$null
    if ($LASTEXITCODE) {
        & $python -m pip install --disable-pip-version-check --quiet --editable $projectRoot
        if ($LASTEXITCODE) { throw 'Unable to install AnimeMachine into the local environment.' }
    }
} else {
    $system = Get-SystemPython
    $releaseVenv = Join-Path $projectRoot '.runtime\windows'
    $venvPython = Join-Path $releaseVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $pythonPrefix = @($system.Prefix)
        & $system.Exe @pythonPrefix -m venv $releaseVenv
        if ($LASTEXITCODE) { throw 'Unable to create the Windows Release environment.' }
    }
    $python = $venvPython
    $pythonPrefix = @()
    & $python -c "import animemachine,httpx,PIL,certifi,truststore" 2>$null
    if ($LASTEXITCODE) {
        $packageDirectory = Join-Path $projectRoot 'packages'
        $wheel = Get-ChildItem -LiteralPath $packageDirectory -Filter 'animemachine-*.whl' -File |
            Sort-Object Name | Select-Object -Last 1
        if (-not $wheel) { throw 'The AnimeMachine wheel is missing from this Release.' }
        & $python -m pip install --disable-pip-version-check --quiet --no-index --find-links $packageDirectory $wheel.FullName
        if ($LASTEXITCODE) {
            & $python -m pip install --disable-pip-version-check --quiet --find-links $packageDirectory $wheel.FullName
            if ($LASTEXITCODE) { throw 'Unable to install AnimeMachine into the Windows Release environment.' }
        }
    }
}

$env:ANM_IMPORTS_DIR = Resolve-AnmPath $env:ANM_IMPORTS_DIR (Join-Path $projectRoot 'imports')
$env:ANM_STATE_DIR = $stateRoot
$env:ANM_CATALOG_DB = $catalog
$env:ANM_RUNTIME_CATALOG_DB = $runtime
$env:ANM_ARCHIVE_DIR = $archive
$env:ANM_METADATA_CACHE_DIR = $cache
$env:ANM_CONFIG_PATH = $config
$env:ANM_WEB_PORT = [string]$Port
$env:ANM_BIND_ADDRESS = $BindAddress
if ($appRoot) {
    $env:PYTHONPATH = $appRoot
} else {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
$env:PYTHONDONTWRITEBYTECODE = '1'

if ($CheckOnly) {
    [pscustomobject]@{
        Python=$python; Config=$config; State=$stateRoot; Catalog=$catalog
        BindAddress=$BindAddress; Port=$Port; Authentication=$env:ANM_AUTH_ENABLED
        Submission=$env:ANM_SUBMISSION_ENABLED
    }
    exit 0
}

$probeHost = Get-AnmProbeHost $BindAddress
$probeUri = "http://${probeHost}:$Port/api/health/live"
foreach ($processId in @(Get-AnmListener $Port)) {
    $verified = $false
    try {
        $health = Invoke-RestMethod -Uri $probeUri -TimeoutSec 3
        $process = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $processId) -ErrorAction Stop
        $commandLine = [string]$process.CommandLine
        $verified = ($health.ok -eq $true -and $health.service -eq 'AnimeMachine' -and
            $commandLine -match '(?i)(?:^|\s)-m\s+animemachine(?:\s|$)' -and $commandLine -match '(?i)(?:^|\s)run(?:\s|$)')
    } catch {
        $verified = $false
    }
    if ($verified) {
        Write-Host "AnimeMachine is already running: http://${probeHost}:$Port"
        exit 0
    }
    throw "Port $Port is occupied by another or unverified process (PID $processId); no process was stopped."
}

Set-Location -LiteralPath $projectRoot
& $python @pythonPrefix -m animemachine run --host $BindAddress --port $Port
exit $LASTEXITCODE
