---
name: model-failover
description: Keep a chat session going when a model provider runs out of credits or hits a rate limit, by switching automatically down an ordered chain (OmniRoute, then OpenRouter, then OpenCode Zen) and returning to the best one when its quota resets. Use when the user asks for automatic model fallback or switching, says credits or free usage ran out, a provider returns 402/429, or asks which provider is serving requests right now.
---

# model-failover

A small local gateway (`failover.py`, stdlib Python, no installs) that speaks the OpenAI
chat-completions API on `http://127.0.0.1:20130/v1`. Clients send model `auto`; the gateway
picks the first healthy tier in `~/.model-failover/tiers.json` and moves on when one fails.

Default chain, in order:

1. **omniroute**, the local OmniRoute router (`localhost:20128`), key in env `OMNIROUTE_API_KEY`
2. **openrouter**, `openrouter/free`, key in env `OPENROUTER_API_KEY` (skipped until the key exists)
3. **opencode**, OpenCode Zen free models, anonymous

## How switching works

- A request goes to the highest tier that is not resting. If it answers 402, 429, 401/403, 404,
  5xx, times out or refuses the connection, the gateway tries the next tier **within the same
  request**, so the client sees no error.
- The failed tier then rests: until the reset time the server states (`Retry-After`,
  `X-RateLimit-Reset`, `reset_seconds`), else 1 h for 429, 6 h for 402, 30 min for auth, 60 s for
  blips. When the rest ends, the next request goes back to the better tier on its own.
- If every tier is resting, it still probes the one that recovers soonest instead of refusing.
- Streaming is supported. A stream that breaks after it began cannot be restarted; the next
  request goes to the next tier.

## Commands (PowerShell, from this folder)

```powershell
./start-gateway.ps1                       # start in background (no-op if running)
python failover.py status                 # active tier + why others are resting
python failover.py test                   # one tiny request through the chain
Get-Content ~\.model-failover\failover.log -Tail 20   # switch history with error detail
```

opencode is already configured (`~/.config/opencode/opencode.jsonc`, model `failover/auto`).
Any other OpenAI-compatible tool: base URL `http://127.0.0.1:20130/v1`, model `auto`, any key.

## When Claude's own 5-hour (or weekly) limit runs out

Claude Code sends its traffic straight to Anthropic, and the endpoint is fixed when it starts,
so a running Claude Code session cannot be switched in mid-flight. What works instead:

```powershell
./claude-free.ps1                    # continue the last conversation in this folder on free models
./claude-free.ps1 -New               # fresh conversation on free models
./claude-free.ps1 --print "question" # one-shot (use --print; PowerShell eats a bare -p)
```

It launches Claude Code against OmniRoute's Anthropic-format endpoint (`/v1/messages`) using the
OmniRoute combo `free-chain` for every model slot, scoped to that one launch. `free-chain` is a
priority list: NVIDIA Nemotron, then `openrouter/free`, then an OpenRouter free model; OmniRoute
moves down it when an entry fails. Tool calls work (tested: it created a file, and a combo with a
broken first entry was served by OpenRouter). Edit the list with `omniroute combo` or the
dashboard. OpenCode Zen is not in this combo (its free quota is exhausted and unreliable).
When the limit resets, just start plain `claude` again.

## Limits to know

- Free quotas reset **daily or by provider rule, not monthly**. OpenRouter `:free` resets at the
  UTC day boundary (50 requests/day until $10 of credits has ever been bought, then 1000).
- The gateway proxies `/v1/chat/completions` only. Claude Code uses `claude-free.ps1` instead.
- Free models differ in tool-calling and context quality; a switch can change answer quality.
- Adding a tier: append an object to `tiers.json` (`name`, `base_url`, `key_env`, `model`).
  Never write keys into that file; put them in environment variables.
- The gateway binds to loopback only. Do not change that.
