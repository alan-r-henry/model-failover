# Run Claude Code on free models, for when your Claude 5-hour (or weekly) limit is used up.
# It goes through the local claude-gateway's /free entry point, so the same rules apply as for
# automatic failover: work-group projects are REFUSED (never sent to a free model).
# Settings apply to this one launch only; nothing global is changed.
#
#   ./claude-free.ps1                              # continue the last chat in this folder, on free models
#   ./claude-free.ps1 -New                         # fresh chat instead
#   ./claude-free.ps1 -Folder C:\proj -Resume <id> # resume a specific session (used by free-resume)
#   ./claude-free.ps1 --print "question"           # extra arguments pass straight to claude
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$New,
    [string]$Folder,
    [string]$Resume,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Rest
)
$dir = if ($Folder) { (Resolve-Path -LiteralPath $Folder).Path } else { (Get-Location).Path }

# Work-group guard (the gateway enforces it too; this gives a clear message before launching).
$cfg = Join-Path $env:USERPROFILE '.model-failover\claude-gateway.json'
$never = @()
if (Test-Path $cfg) { try { $never = @((Get-Content $cfg -Raw | ConvertFrom-Json).never_free_dirs | ForEach-Object { $_.ToLower() }) } catch {} }
foreach ($n in $never) {
    if ($dir.ToLower().Contains($n)) { Write-Error "Refusing: '$dir' is a protected work-group project ('$n'). Free models are never used for it."; return }
}

# Make sure the gateway is up (starts OmniRoute + gateways if needed).
if (-not (Get-NetTCPConnection -LocalPort 20135 -State Listen -ErrorAction SilentlyContinue)) {
    & (Join-Path $PSScriptRoot 'autostart.ps1')
    $end = (Get-Date).AddSeconds(60)
    while (-not (Get-NetTCPConnection -LocalPort 20135 -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $end) { Start-Sleep 2 }
}
if (-not (Get-NetTCPConnection -LocalPort 20135 -State Listen -ErrorAction SilentlyContinue)) {
    Write-Error 'claude-gateway is not running on 127.0.0.1:20135 and could not be started.'; return
}

# Child-process environment only. The token is a placeholder: the gateway uses its own key.
$env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:20135/free'
$env:ANTHROPIC_AUTH_TOKEN = 'local-free-mode'
$env:ANTHROPIC_API_KEY = ''
$model = 'free-chain'
$env:ANTHROPIC_MODEL = $model
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $model
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $model
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'

$a = @()
if ($Resume) { $a += '--resume'; $a += $Resume }
elseif (-not $New -and -not ($Rest -contains '-p') -and -not ($Rest -contains '--print')) { $a += '--continue' }
Push-Location $dir
try { claude @a @Rest } finally { Pop-Location }
