# Restarts the Claude gateway (20135/20136) and the model-failover service (20130), hidden.
# Stops EVERY running copy first (found by command line, not by port), then starts exactly one of each.
$dir = "$env:USERPROFILE\.claude\skills\model-failover"
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"
$old = Get-CimInstance Win32_Process -Filter "name like 'python%'" |
    Where-Object { $_.CommandLine -match 'claude-gateway\.py|failover\.py' }
$old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
for ($i = 0; $i -lt 20 -and (Get-NetTCPConnection -LocalPort 20135, 20136, 20130 -State Listen -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Milliseconds 250
}
Start-Process $py -ArgumentList '-u', "`"$dir\failover.py`"", 'serve', '--port', '20130' -WindowStyle Hidden
Start-Process $py -ArgumentList '-u', "`"$dir\claude-gateway.py`"", 'serve', '--port', '20135' -WindowStyle Hidden
for ($i = 0; $i -lt 40; $i++) {
    $up = @(20130, 20135, 20136 | Where-Object { Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue })
    if ($up.Count -eq 3) {
        $n = @(Get-CimInstance Win32_Process -Filter "name like 'python%'" | Where-Object { $_.CommandLine -match 'claude-gateway\.py|failover\.py' }).Count
        "restarted: stopped $(@($old).Count) old process(es); $n running now (expected 2)"; exit 0
    }
    Start-Sleep -Milliseconds 250
}
"services did not all come back within 10s; check $env:USERPROFILE\.model-failover\claude-gateway.log"; exit 1
