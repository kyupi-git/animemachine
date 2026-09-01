[CmdletBinding()]
param([switch]$IncludeBuilds)
$ErrorActionPreference = 'Stop'
$releaseLayout = (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'app\animemachine') -PathType Container) -or
    (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'packages') -PathType Container)
$root = if ($releaseLayout) {
    $PSScriptRoot.TrimEnd('\')
} else {
    [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
}
$directories = @('.pytest_cache','build')
if ($IncludeBuilds) { $directories += 'dist' }
foreach ($relative in $directories) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if ($target.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
Get-ChildItem -LiteralPath $root -Directory -Recurse -Force |
    Where-Object { $_.Name -in @('__pycache__','.mypy_cache','.ruff_cache') -and $_.FullName -notlike "$root\.local\state\*" } |
    Sort-Object FullName -Descending | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $root -Directory -Recurse -Force -Filter '*.egg-info' |
    Sort-Object FullName -Descending | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $root -File -Recurse -Force |
    Where-Object { $_.Extension -in @('.pyc','.pyo') -or $_.Name -match '\.(tmp|part)$' } |
    Where-Object { $_.FullName -notlike "$root\.local\state\*" } |
    Remove-Item -Force
Write-Host 'Temporary files were removed. Databases, cover cache, credentials and history were preserved.'
