[CmdletBinding()]
param([string]$Version = '', [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $root 'dist' }
$python = (Get-Command python.exe -ErrorAction Stop).Source
$requiredPython = [IO.File]::ReadAllText((Join-Path $root 'RELEASE_PYTHON_VERSION')).Trim()
$actualPython = (& $python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")').Trim()
if ($actualPython -ne $requiredPython) {
    throw "Official Windows Release requires Python $requiredPython; found $actualPython at $python."
}
$canonicalVersion = (& $python (Join-Path $root 'scripts\build_info.py') --print-version).Trim()
& $python (Join-Path $root 'scripts\build_info.py') --check-repository
if ($LASTEXITCODE) { throw 'Repository version metadata is inconsistent.' }
if ($Version -and $Version -ne $canonicalVersion) {
    throw "Requested version $Version does not match VERSION $canonicalVersion."
}
$Version = $canonicalVersion
$stage = Join-Path ([IO.Path]::GetTempPath()) ('AnimeMachine-release-' + [guid]::NewGuid().ToString('N'))
$release = Join-Path $stage "AnimeMachine-$Version"
New-Item -ItemType Directory -Force -Path $release,(Join-Path $release 'app'),(Join-Path $release 'packages'),(Join-Path $release 'config'),(Join-Path $release 'imports'),(Join-Path $release 'data'),(Join-Path $release 'docs'),$OutputDirectory | Out-Null
try {
    foreach ($generated in @((Join-Path $root 'build'), (Join-Path $root 'src\animemachine.egg-info'))) {
        $full = [IO.Path]::GetFullPath($generated)
        if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean path outside the repository: $full"
        }
        if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
    }
    $pythonRoot = Split-Path -Parent $python
    $runtime = Join-Path $release 'runtime'
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    foreach ($pattern in @('python.exe','python*.dll','vcruntime*.dll')) {
        Get-ChildItem -LiteralPath $pythonRoot -Filter $pattern -File | Copy-Item -Destination $runtime
    }
    foreach ($directory in @('DLLs','Lib')) {
        $source = Join-Path $pythonRoot $directory
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $runtime $directory) -Recurse }
    }
    foreach ($relative in @('Lib\test','Lib\idlelib\idle_test','Lib\site-packages','Lib\tkinter','Lib\turtledemo','Lib\venv')) {
        $candidate = Join-Path $runtime $relative
        if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Recurse -Force }
    }
    & $python -m pip install --disable-pip-version-check --no-compile --target (Join-Path $release 'app') $root
    if ($LASTEXITCODE) { throw 'Unable to install AnimeMachine into Release staging.' }
    & $python -m pip wheel --disable-pip-version-check --no-deps --wheel-dir (Join-Path $release 'packages') $root
    if ($LASTEXITCODE) { throw 'Unable to build the portable AnimeMachine wheel.' }
    $sourcePackage = Join-Path $root 'src\animemachine'
    $installedPackage = Join-Path $release 'app\animemachine'
    foreach ($sourceFile in Get-ChildItem -LiteralPath $sourcePackage -Recurse -File) {
        if ($sourceFile.FullName -like '*\__pycache__\*' -or $sourceFile.Extension -eq '.pyc') { continue }
        # System PowerShell 5.1 runs on .NET Framework, which does not expose
        # Path.GetRelativePath.  Both paths are already absolute descendants.
        $relative = $sourceFile.FullName.Substring($sourcePackage.Length).TrimStart('\', '/')
        $installedFile = Join-Path $installedPackage $relative
        if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf) -or
            (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash) {
            throw "Release package does not match current source: $relative"
        }
    }

    Copy-Item -LiteralPath (Join-Path $root 'config\config.example.json') -Destination (Join-Path $release 'config')
    Copy-Item -LiteralPath (Join-Path $root 'config\config.schema.json') -Destination (Join-Path $release 'config')
    [IO.File]::WriteAllText((Join-Path $release 'imports\README.txt'), 'Place a verified Bangumi Archive dump-*.zip here, then start AnimeMachine.', [Text.UTF8Encoding]::new($false))
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'AnimeMachine.ps1') -Destination (Join-Path $release 'AnimeMachine.ps1')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'AnimeMachine.cmd') -Destination (Join-Path $release 'AnimeMachine.cmd')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Clean-AnimeMachine.ps1') -Destination (Join-Path $release 'Clean-AnimeMachine.ps1')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Clean-AnimeMachine.cmd') -Destination (Join-Path $release 'Clean-AnimeMachine.cmd')
    foreach ($name in @('AnimeMachine.sh','AnimeMachine-Linux.sh','AnimeMachine-macOS.command','Clean-AnimeMachine.sh')) {
        Copy-Item -LiteralPath (Join-Path $root "scripts\unix\$name") -Destination (Join-Path $release $name)
    }
    Copy-Item -LiteralPath (Join-Path $root 'deploy\local\.env.local.example') -Destination (Join-Path $release '.env.local.example')
    foreach ($name in @('README.md','README.en.md','README.ja.md','LICENSE','THIRD-PARTY.md','SECURITY.md','CONTRIBUTING.md')) {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination $release
    }
    Get-ChildItem -LiteralPath (Join-Path $root 'docs') -Filter '*.md' -File |
        Copy-Item -Destination (Join-Path $release 'docs')
    $images = Join-Path $root 'docs\images'
    if (Test-Path -LiteralPath $images -PathType Container) {
        Copy-Item -LiteralPath $images -Destination (Join-Path $release 'docs\images') -Recurse
    }
    Copy-Item -LiteralPath (Join-Path $root 'VERSION') -Destination $release
    Copy-Item -LiteralPath (Join-Path $root 'RELEASE_PYTHON_VERSION') -Destination $release
    & $python (Join-Path $root 'scripts\build_info.py') --output (Join-Path $release 'BUILD-INFO.json') --build-type windows-portable --platform windows --python-version (& $python -c 'import platform; print(platform.python_version())')
    if ($LASTEXITCODE) { throw 'Unable to generate BUILD-INFO.json.' }
    Get-ChildItem -LiteralPath $release -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $release -Recurse -File | Where-Object { $_.Extension -in '.pyc','.sqlite','.sqlite3','.db','.db3' } | Remove-Item -Force
    & $python (Join-Path $root 'scripts\check_public_tree.py') $release
    if ($LASTEXITCODE) { throw 'Release staging contains private or runtime content.' }

    $archive = Join-Path $OutputDirectory "AnimeMachine-$Version-release-windows.zip"
    & $python (Join-Path $root 'scripts\release_zip.py') $release $archive
    if ($LASTEXITCODE) { throw 'Unable to create the Release archive.' }
    & $python (Join-Path $root 'scripts\check_public_tree.py') $archive
    if ($LASTEXITCODE) { throw 'Release archive contains private or runtime content.' }
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $line = "$hash  $([IO.Path]::GetFileName($archive))" + [Environment]::NewLine
    [IO.File]::WriteAllText("$archive.sha256", $line, [Text.UTF8Encoding]::new($false))
    Write-Host "Release: $archive"
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
