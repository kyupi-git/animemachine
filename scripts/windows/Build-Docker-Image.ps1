[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$Image = 'ghcr.io/kyupi-git/animemachine',
    [switch]$NoExport
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = (Get-Command python.exe -ErrorAction Stop).Source
$canonicalVersion = (& $python (Join-Path $root 'scripts\build_info.py') --print-version).Trim()
& $python (Join-Path $root 'scripts\build_info.py') --check-repository
if ($LASTEXITCODE) { throw 'Repository version metadata is inconsistent.' }
if ($Version -and $Version -ne $canonicalVersion) { throw "Requested version $Version does not match VERSION $canonicalVersion." }
$Version = $canonicalVersion
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) { throw 'Docker CLI was not found.' }
$tag = $Image + ':' + $Version

& docker build --build-arg "ANM_VERSION=$Version" -t $tag -t ($Image + ':latest') -f (Join-Path $root 'packaging\docker\Dockerfile') $root
if ($LASTEXITCODE) { throw 'Docker image build failed.' }
if ($NoExport) {
    Write-Host "Image: $tag"
    exit 0
}

$archive = Join-Path $dist "AnimeMachine-$Version-image.tar"
& docker save --output $archive $tag
if ($LASTEXITCODE) { throw 'Docker image export failed.' }
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$line = "$hash  $([IO.Path]::GetFileName($archive))" + [Environment]::NewLine
[IO.File]::WriteAllText("$archive.sha256", $line, [Text.UTF8Encoding]::new($false))

$bundle = Join-Path $dist "AnimeMachine-$Version-compose"
if (Test-Path -LiteralPath $bundle) { Remove-Item -LiteralPath $bundle -Recurse -Force }
New-Item -ItemType Directory -Force -Path $bundle | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'deploy\compose\templates\04-full-stack\compose.yaml') -Destination $bundle
Copy-Item -LiteralPath (Join-Path $root 'deploy\compose\templates\04-full-stack\.env.example') -Destination $bundle
foreach ($name in @('README.md','README.en.md','README.ja.md','CHANGELOG.md','CHANGELOG.en.md','CHANGELOG.ja.md','LICENSE','THIRD-PARTY.md','SECURITY.md','CONTRIBUTING.md')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $bundle
}
New-Item -ItemType Directory -Force -Path (Join-Path $bundle 'docs') | Out-Null
Get-ChildItem -LiteralPath (Join-Path $root 'docs') -Filter '*.md' -File |
    Copy-Item -Destination (Join-Path $bundle 'docs')
& $python (Join-Path $root 'scripts/copy_document_images.py') (Join-Path $root 'docs/images') (Join-Path $bundle 'docs/images')
if ($LASTEXITCODE) { throw 'Unable to copy documentation images.' }
foreach ($file in @((Join-Path $bundle 'compose.yaml'), (Join-Path $bundle '.env.example'))) {
    $text = [regex]::Replace([IO.File]::ReadAllText($file), 'ghcr\.io/kyupi-git/animemachine:\d+\.\d+\.\d+', $tag)
    [IO.File]::WriteAllText($file, $text, [Text.UTF8Encoding]::new($false))
}
Write-Host "Image archive: $archive"
Write-Host "Compose bundle: $bundle"
