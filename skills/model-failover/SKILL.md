---
name: model-failover
description: Keep a chat session going when a model provider runs out of credits or hits a rate limit, by switching automatically down an ordered chain (OmniRoute, then OpenRouter, then OpenCode Zen) and returning to the best one when its quota resets. Also routes Claude Code chats to free models when the Claude usage limit is reached or when the user picks the free model. Use when the user asks for automatic model fallback or switching, says credits or free usage ran out, a provider returns 402/429, asks which provider is serving requests right now, or wants a chat moved to or off free models.
---

# model-failover

Two local, loopback-only services (stdlib Python, no installs):

| Service | Port | For |
|---|---|---|
| `failover.py` | 20130 | OpenAI-compatible clients (opencode etc.): model `auto` walks a provider chain |
| `claude-gateway.py` | 20135 (base URL) + 20136 (HTTPS proxy) | Claude Code: Claude first, free models when allowed |

Config and logs live in `~/.model-failover/`. Never write API keys into files there or here; keys
come from environment variables (`OMNIROUTE_API_KEY`, `OPENROUTER_API_KEY`).

## failover.py (OpenAI clients)

Clients send model `auto`; the gateway uses the first healthy tier in `~/.model-failover/tiers.json`:
OmniRoute (`localhost:20128`), then `openrouter/free`, then OpenCode Zen. On 402, 429, 401/403, 404,
5xx, timeout or refused connection it retries the next tier **in the same request**. The failed tier
rests until the reset the server states (`Retry-After`, `X-RateLimit-Reset`, `reset_seconds`), else
1 h for 429, 6 h for 402, 30 min for auth, 60 s for blips. If every tier rests, it probes the one that
recovers soonest.

```powershell
python failover.py status      # active tier and why others rest
python failover.py test        # one tiny request through the chain
```

## claude-gateway.py (Claude Code, terminal and desktop app)

All Claude Code traffic reaches the gateway through `~/.claude/settings.json`:

```json
"env": { "HTTPS_PROXY": "http://127.0.0.1:20136",
         "NODE_EXTRA_CA_CERTS": "C:\\Users\\<you>\\.model-failover\\tls\\api-anthropic.pem" },
"modelOverrides": { "claude-opus-4-6": "free" }
```

The proxy on 20136 intercepts only `api.anthropic.com` (with a local self-signed certificate that only
Claude Code trusts, through `NODE_EXTRA_CA_CERTS`) and tunnels every other host untouched. This is what
lets desktop-app chats use the gateway, since the app overrides `ANTHROPIC_BASE_URL`.

### When a request goes to free models

1. **The user picked the free model** (Opus 4.6 in the model picker, mapped to `free` by
   `modelOverrides`): always free, in any chat. In a work-folder chat the reply note warns that the
   conversation goes to the free providers. If the free chain fails, Claude answers instead.
2. **Claude's usage limit is reached** (429/402 with a long reset, or limit wording): only chats on
   the **free list** move to free automatically, until the reset. Every other chat gets Anthropic's
   own limit reply and the user can switch it to Opus 4.6.
3. Chats whose working folder matches `never_free_dirs` never move automatically, even if listed.

The first reply on a new backend starts with a one-line note ("Free model in use ...", "This chat
isn't on your free list ...", "Work project ..."). Anthropic credentials are never sent to the free tier.

`~/.model-failover/claude-gateway.json`:

```json
{ "never_free_dirs": ["your-work-repo"],     // folder-name substrings: no automatic failover
  "free_sessions":   ["<session id>", ...],  // the free list (managed by migrate-sessions.py)
  "fallback_model":  "claude-opus-5-5" }     // Claude model used when the free chain fails
```

### Free list (opt-in)

```powershell
python migrate-sessions.py                                        # list chats, environment, model
python migrate-sessions.py --free-set "youtube manager" --apply   # exactly these chats are free
python migrate-sessions.py --free-add "spotify" --apply
python migrate-sessions.py --free-remove "youtube" --apply
python migrate-sessions.py --undo                                 # restore the last --apply
```

Names match part of a chat title. `--free-*` saves the free list and moves the named desktop chats to
the "Free Chat" environment on Opus 4.6; chats taken off it get back the model they had before. It
edits the desktop app's own chat files, so **quit the app first** (the script refuses otherwise).
Work-folder chats are never added. A forked or new session has a new id: add it again.

### What the gateway fixes in free replies

- Strips Anthropic-signed thinking blocks and Claude-only request fields.
- **Message threads:** Claude Code may send only "continue thread X" plus new turns, with tools and
  history stored at Anthropic. The free chain has neither, so the gateway answers
  `thread_unsupported_request`; Claude Code then resends full requests for that session.
- **Tool calls written as text** (`<tool_call><function=Bash>...`, `<tool_call>{json}`, or
  `<invoke name="..."><parameter name="...">` in any wrapper) are converted into real `tool_use`
  blocks for tools the request declared; earlier such turns in the history are replaced by a note so
  the model stops copying them. Because of this, free replies with tools are buffered, not streamed.
  An unconvertible one is saved to `~/.model-failover/last-unrepaired-free-reply.txt`.

### Running it

```powershell
powershell -ExecutionPolicy Bypass -File restart-gateway.ps1   # stop every copy, start one of each
python claude-gateway.py status                                 # counters, free list, work folders
Get-Content ~\.model-failover\claude-gateway.log -Tail 20
```

`launch-hidden.vbs` starts OmniRoute and both services with no window; run it from a login task
(optionally repeating every 10 minutes as a watchdog: each service refuses a port that is already
taken, so repeats are harmless). Because every Claude Code chat goes through the proxy, **if the
gateway is down Claude Code cannot connect**: restart it, or remove `HTTPS_PROXY` from settings.json.

### Manual helpers

- `claude-free.ps1 [-New] [-Folder <dir> -Resume <id>] [--print "q"]`: run one terminal session on
  free models through `/free`.
- `free-resume.py`: pick a recent chat and reopen it on free models with `claude-free.ps1`.
- `claude-failover.ps1 -Enable/-Disable <folder> | -GlobalOn | -GlobalOff | -Status`: older
  base-URL routing for terminal sessions.

## Limits

- Free quotas reset daily or by provider rule, not monthly.
- Free models have smaller context windows and weaker tool use; `/compact` long chats.
- Switching a chat between Claude and free models loses Claude's prompt cache for it.
- Regular claude.ai chats (the Chat tab) run on Anthropic's servers and cannot be routed.
- The real Anthropic limit reply has only been simulated; detection uses reset headers and limit
  wording. Check the log after a real limit.
- Keep both services on loopback.
