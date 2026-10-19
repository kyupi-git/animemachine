[CmdletBinding()]
param([switch]$IncludeBuilds)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$releaseLayout = Test-Path -LiteralPath (Join-Path $root 'VERSION') -PathType Leaf
if (-not $releaseLayout) { throw 'Application directory not found.' }
$python = if (Test-Path -LiteralPath (Join-Path $root 'runtime/python.exe')) {
    Join-Path $root 'runtime/python.exe'
} elseif (Test-Path -LiteralPath (Join-Path $root '.venv/Scripts/python.exe')) {
    Join-Path $root '.venv/Scripts/python.exe'
} else { (Get-Command python -ErrorAction Stop).Source }
$cleaner = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'clean_runtime.py')) {
    Join-Path $PSScriptRoot 'clean_runtime.py'
} else { Join-Path $root 'scripts/clean_runtime.py' }
$arguments = @($cleaner, '--root', $root)
if ($IncludeBuilds) { $arguments += '--include-builds' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Application cache cleanup failed.' }
