# Run at login: bring up OmniRoute (tier 1) and the model-failover gateway, both loopback-only.
# Safe to run repeatedly; each part is skipped if already listening.
$ErrorActionPreference = 'SilentlyContinue'
$env:OMNIROUTE_SERVER_HOST = '127.0.0.1'   # never expose OmniRoute to the network

function Wait-Port($port, $sec) {
    $end = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $end) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { return $true }
        Start-Sleep 2
    }
    return $false
}

if (-not (Get-NetTCPConnection -LocalPort 20128 -State Listen -ErrorAction SilentlyContinue)) {
    # Use the .cmd shim: Get-Command finds omniroute.ps1, which Start-Process cannot launch.
    $om = Join-Path $env:APPDATA 'npm\omniroute.cmd'
    Start-Process -FilePath $om -ArgumentList 'serve', '--daemon', '--no-open' -WindowStyle Hidden
    $null = Wait-Port 20128 90
}
& (Join-Path $PSScriptRoot 'start-gateway.ps1')
