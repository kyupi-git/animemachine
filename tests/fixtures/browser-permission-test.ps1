param([string]$SourcePath, [string]$CommandPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:fakeRegistry = @{}
$script:valueName = 'AutoLaunchProtocolsFromOrigins'
$initial = '[{"protocol":"spotify","allowed_origins":["https://music.example"]},{"protocol":"potplayer","allowed_origins":["http://192.0.2.9:8787"]}]'
foreach ($browser in @('Microsoft\Edge','Google\Chrome')) {
    $script:fakeRegistry[(Join-Path 'HKCU:\Software\Policies' $browser)] = $initial
}
# These test doubles capture all policy operations; real browser settings stay untouched.
function Test-Path { param([string]$LiteralPath) return $script:fakeRegistry.ContainsKey($LiteralPath) }
function Get-ItemProperty { param([string]$LiteralPath) return [pscustomobject]@{ AutoLaunchProtocolsFromOrigins = $script:fakeRegistry[$LiteralPath] } }
function New-Item { param([string]$Path,[switch]$Force) }
function New-ItemProperty {
    param([string]$LiteralPath,[string]$Name,[string]$Value,[string]$PropertyType,[switch]$Force)
    if ($Name -ne $script:valueName -or $PropertyType -ne 'String') { throw 'Unexpected registry write' }
    $script:fakeRegistry[$LiteralPath] = $Value
}
foreach ($file in @($SourcePath,$CommandPath)) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$parseErrors) | Out-Null
    if ($parseErrors.Count) { throw 'Generated PowerShell failed to parse' }
}
. $CommandPath
. $SourcePath -Origin 'http://192.0.2.4:8787'
foreach ($value in $script:fakeRegistry.Values) {
    $parsedRules = ConvertFrom-Json -InputObject $value
    $rules = @($parsedRules)
    if ($rules.Count -ne 3) { throw 'An existing rule was lost or duplicated' }
    $spotify = @($rules | Where-Object { $_.protocol -eq 'spotify' })[0]
    if ($spotify.allowed_origins.Count -ne 1 -or $spotify.allowed_origins[0] -ne 'https://music.example') { throw 'Unrelated permissions changed' }
    $pot = @($rules | Where-Object { $_.protocol -eq 'potplayer' })[0]
    if ($pot.allowed_origins.Count -ne 2) { throw 'Player permissions were not preserved' }
    foreach ($protocol in @('potplayer','vlc')) {
        $rule = @($rules | Where-Object { $_.protocol -eq $protocol })[0]
        if ($rule.allowed_origins -notcontains 'http://192.0.2.4:8787') { throw 'Site was not allowed' }
    }
}
Write-Host 'Policy merge, generated command, parser and repeat execution passed without registry writes.'
