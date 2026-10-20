param([Parameter(Mandatory)][uri]$Origin)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Origin.Scheme -notin @('http','https') -or $Origin.UserInfo -or $Origin.AbsolutePath -ne '/' -or $Origin.Query -or $Origin.Fragment) {
    throw 'Use the AnimeMachine site address, including its port.'
}
$site = $Origin.GetLeftPart([System.UriPartial]::Authority)
$pattern = if ($Origin.HostNameType -eq [System.UriHostNameType]::Dns) { "$($Origin.Scheme)://.$($Origin.Authority)" } else { $site }
$name = 'AutoLaunchProtocolsFromOrigins'
$changes = @()
foreach ($browser in @('Microsoft\Edge','Google\Chrome')) {
    $key = Join-Path 'HKCU:\Software\Policies' $browser
    $existing = if (Test-Path -LiteralPath $key) { Get-ItemProperty -LiteralPath $key } else { $null }
    $entries = @()
    if ($existing -and $existing.PSObject.Properties[$name]) {
        if (-not $existing.$name.TrimStart().StartsWith('[')) { throw "Existing $browser policy is not a JSON array; it was preserved." }
        $parsed = ConvertFrom-Json -InputObject $existing.$name
        if ($null -ne $parsed) { $entries = @($parsed) }
        foreach ($item in $entries) {
            if ($item -isnot [pscustomobject] -or !$item.PSObject.Properties['protocol'] -or !$item.PSObject.Properties['allowed_origins'] -or $item.allowed_origins -isnot [array]) {
                throw "Existing $browser policy has an unexpected format; it was preserved."
            }
        }
    }
    foreach ($protocol in @('potplayer','vlc')) {
        $entry = @($entries | Where-Object { $_.protocol -ceq $protocol })
        if ($entry.Count) {
            foreach ($item in $entry) { $item.allowed_origins = @(@($item.allowed_origins) + $pattern | Select-Object -Unique) }
        } else { $entries += [pscustomobject]@{ protocol = $protocol; allowed_origins = @($pattern) } }
    }
    $changes += @{ Key = $key; Value = ConvertTo-Json -InputObject @($entries) -Depth 20 -Compress }
}
foreach ($change in $changes) {
    New-Item -Path $change.Key -Force | Out-Null
    New-ItemProperty -LiteralPath $change.Key -Name $name -Value $change.Value -PropertyType String -Force | Out-Null
}
Write-Host "Allowed this site to open PotPlayer and VLC: $site. Restart Edge or Chrome to apply."
