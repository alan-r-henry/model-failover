# Turn the Claude-limit failover on or off for ONE project folder.
# It adds/removes ANTHROPIC_BASE_URL in <folder>\.claude\settings.local.json, so only sessions
# opened in that folder go through the local claude-gateway; every other project is untouched.
#
#   ./claude-failover.ps1 -Enable  C:\path\to\project
#   ./claude-failover.ps1 -Disable C:\path\to\project
#   ./claude-failover.ps1 -Status                      # is the gateway up, and what is it serving
#   ./claude-failover.ps1 -GlobalOn                    # all terminal Claude Code sessions (user settings.json)
#   ./claude-failover.ps1 -GlobalOff                   # undo that (works even if the gateway is down)
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Enable,
    [string]$Disable,
    [switch]$GlobalOn,
    [switch]$GlobalOff,
    [switch]$Status
)
$url = 'http://127.0.0.1:20135'

if ($GlobalOn -or $GlobalOff) {
    $file = Join-Path $env:USERPROFILE '.claude\settings.json'
    $obj = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    Copy-Item -LiteralPath $file ($file + '.bak-failover') -Force
    if (-not $obj.PSObject.Properties['env']) { $obj | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) }
    if ($GlobalOn) {
        if (-not (Get-NetTCPConnection -LocalPort 20135 -State Listen -ErrorAction SilentlyContinue)) {
            Write-Error 'claude-gateway is not running on 127.0.0.1:20135; start it first (autostart.ps1). Not changing settings.'; return }
        $obj.env | Add-Member -NotePropertyName ANTHROPIC_BASE_URL -NotePropertyValue $url -Force
    } else { $obj.env.PSObject.Properties.Remove('ANTHROPIC_BASE_URL') }
    $obj | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $file -Encoding UTF8
    Write-Output ("global failover: {0} ({1}; backup {1}.bak-failover)" -f $(if ($GlobalOn) { 'ON' } else { 'OFF' }), $file)
    return
}

function Edit-Settings($folder, $on) {
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) { Write-Error "Folder not found: $folder"; return }
    $dir = Join-Path $folder '.claude'
    $file = Join-Path $dir 'settings.local.json'
    $obj = if (Test-Path -LiteralPath $file) { Get-Content -LiteralPath $file -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
    if (-not $obj.PSObject.Properties['env']) { $obj | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) }
    if ($on) {
        $obj.env | Add-Member -NotePropertyName ANTHROPIC_BASE_URL -NotePropertyValue $url -Force
    } else {
        $obj.env.PSObject.Properties.Remove('ANTHROPIC_BASE_URL')
        if (-not @($obj.env.PSObject.Properties).Count) { $obj.PSObject.Properties.Remove('env') }
    }
    New-Item -ItemType Directory -Force $dir | Out-Null
    $obj | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $file -Encoding UTF8
    Write-Output ("{0}: {1}" -f $(if ($on) { 'ENABLED ' } else { 'disabled' }), $file)
}

if ($Enable) {
    if (-not (Get-NetTCPConnection -LocalPort 20135 -State Listen -ErrorAction SilentlyContinue)) {
        Write-Warning 'claude-gateway is not running on 127.0.0.1:20135. Start it first (autostart.ps1) or Claude Code in that folder cannot connect.'
    }
    Edit-Settings $Enable $true
}
if ($Disable) { Edit-Settings $Disable $false }
if ($Status -or (-not $Enable -and -not $Disable)) {
    python (Join-Path $PSScriptRoot 'claude-gateway.py') status
}
