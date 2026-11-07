# model-failover

A [Claude Code](https://claude.com/claude-code) skill and a small local gateway that keep you working when a model provider runs out of credits or hits a rate limit. It walks an ordered chain of providers and moves to the next one automatically:

1. **OmniRoute** (local router, `localhost:20128`)
2. **OpenRouter** (`openrouter/free`)
3. **OpenCode Zen** (free models)

When a provider answers "out of credits", "rate limited", "key rejected" or "unreachable", the request is retried on the next provider **in the same call**, so the client sees no error. The failed provider rests until its quota resets (using the reset time the provider reports) and the chain returns to it on its own.

Windows + PowerShell + Python 3 (standard library only, no installs).

## What is in here

| File | Purpose |
|---|---|
| `skills/model-failover/failover.py` | The gateway: an OpenAI-compatible `/v1/chat/completions` proxy on `127.0.0.1:20130`. Client model `auto`. |
| `skills/model-failover/start-gateway.ps1` | Starts the gateway in the background. |
| `skills/model-failover/autostart.ps1` | Starts OmniRoute and the gateway (both loopback-only); meant for a login task. |
| `skills/model-failover/claude-free.ps1` | Runs Claude Code on free models through OmniRoute when your Claude 5-hour or weekly limit is used up. |
| `skills/model-failover/SKILL.md` | The skill definition for Claude Code. |

## Setup

1. Install [OmniRoute](https://github.com/cheaper-inference/OmniRoute) (`npm install -g omniroute`), start it bound to loopback (`OMNIROUTE_SERVER_HOST=127.0.0.1`), and create an API key in its dashboard.
2. Put keys in **environment variables**, never in files: `OMNIROUTE_API_KEY`, and `OPENROUTER_API_KEY` for the OpenRouter tier. A tier with no key is skipped.
3. Copy `skills/model-failover` to `~/.claude/skills/model-failover`, then run `start-gateway.ps1`.
4. Point any OpenAI-compatible client (for example opencode) at `http://127.0.0.1:20130/v1` with model `auto`.

The tier list lives in `~/.model-failover/tiers.json` (created on first run). Each tier names the environment variable that holds its key.

```powershell
python failover.py status   # active tier, and why others are resting
python failover.py test     # one tiny request through the chain
```

## When your Claude limit runs out

Claude Code talks straight to Anthropic and its endpoint is fixed at launch, so a running session cannot be switched mid-flight. Instead, close it and run:

```powershell
./claude-free.ps1            # continue the last conversation here, on free models
./claude-free.ps1 -New       # fresh conversation
```

This uses an OmniRoute combo called `free-chain` (create it with `omniroute combo create free-chain --strategy priority --models "<provider/model>,<provider/model>"`). The settings apply to that one launch only. Start plain `claude` again once your limit resets.

## Behaviour and limits

- Failure handling: 402 rests 6 h; 429 rests until the provider's stated reset (else 1 h); 401/403 rests 30 min; 5xx, timeouts and refused connections rest 60 s. If every tier is resting it still probes the one that recovers soonest.
- A client disconnecting mid-stream is not counted against a provider.
- A stream that breaks after it has started cannot be restarted; the next request uses the next tier.
- Free quotas reset daily or by each provider's own rule, **not monthly**. OpenRouter `:free` models: 20 requests/min, 50/day until $10 of credits has ever been bought, then 1000/day.
- Free models differ in tool-calling and context quality, so a switch can change answer quality.
- The gateway proxies `/v1/chat/completions` only. It binds to loopback; do not expose it.
- Tested: failover on simulated 402/429 responses (normal and streaming), a real switch to OpenRouter, and Claude Code running a tool call through OmniRoute on free models. Not tested: long-running sessions or other clients.
- Using login-based routes such as Kiro or Antigravity inside OmniRoute may breach those services' terms; they are not part of this chain.

## License

[MIT](LICENSE)
