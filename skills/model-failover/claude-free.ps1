# Run Claude Code on free models through OmniRoute, for when your Claude 5-hour (or weekly)
# limit is used up. The settings apply to this one launch only; nothing global is changed.
#
#   ./claude-free.ps1                 # continue the most recent conversation in this folder, on free models
#   ./claude-free.ps1 -New            # start a fresh conversation instead
#   ./claude-free.ps1 -Model <id>     # pick another OmniRoute model id
#   ./claude-free.ps1 -p "question"   # extra arguments pass straight to claude
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$New,
    # OmniRoute combo "free-chain": NVIDIA Nemotron first, then OpenRouter free models.
    [string]$Model = 'free-chain',
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Rest
)
$key = [Environment]::GetEnvironmentVariable('OMNIROUTE_API_KEY', 'User')
if (-not $key) { Write-Error 'OMNIROUTE_API_KEY is not set (create a key in the OmniRoute dashboard).'; return }
try { Invoke-RestMethod http://localhost:20128/v1/models -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 10 | Out-Null }
catch { Write-Error 'OmniRoute is not answering on localhost:20128. Start it with: omniroute serve --daemon --no-open'; return }

# Child-process environment only: these vanish when claude exits.
$env:ANTHROPIC_BASE_URL = 'http://localhost:20128'
$env:ANTHROPIC_AUTH_TOKEN = $key
$env:ANTHROPIC_API_KEY = ''
$env:ANTHROPIC_MODEL = $Model
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $Model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $Model
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $Model
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'

$args2 = @()
if (-not $New -and -not ($Rest -contains '-p') -and -not ($Rest -contains '--print')) { $args2 += '--continue' }
claude @args2 @Rest
