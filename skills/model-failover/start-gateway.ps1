# Starts the model-failover gateway in the background if it is not already running.
$port = 20130
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Output "model-failover already running on 127.0.0.1:$port"; return
}
$py = (Get-Command python -ErrorAction Stop).Source
Start-Process $py -ArgumentList '-u', (Join-Path $PSScriptRoot 'failover.py'), 'serve', '--port', $port -WindowStyle Hidden
Start-Sleep 2
Write-Output "model-failover started on 127.0.0.1:$port"
