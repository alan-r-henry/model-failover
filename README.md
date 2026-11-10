# model-failover

A [Claude Code](https://claude.com/claude-code) skill and two small local services that keep you working when a model provider runs out of credits or your Claude usage limit is reached.

- **`failover.py`**: an OpenAI-compatible gateway for tools like opencode. It walks an ordered chain of free providers (OmniRoute, then OpenRouter, then OpenCode Zen) and moves to the next one inside the same request when one answers "out of credits", "rate limited" or "unreachable".
- **`claude-gateway.py`**: sits between Claude Code (terminal and the desktop app's Code tab) and Anthropic. Claude answers as usual. A chat moves to free models when you pick the free model in the model picker, or automatically when Claude's limit is hit and you have put that chat on your free list. Work folders you list are never moved automatically.

Windows + PowerShell + Python 3 (standard library only). Free models come from a local [OmniRoute](https://github.com/cheaper-inference/OmniRoute) combo named `free-chain`.

## What is in here

| File (`skills/model-failover/`) | Purpose |
|---|---|
| `SKILL.md` | Instructions Claude Code reads when the skill is used. |
| `failover.py` | OpenAI-compatible `/v1/chat/completions` gateway on `127.0.0.1:20130`, model `auto`. |
| `claude-gateway.py` | Claude Code gateway: base URL on `127.0.0.1:20135`, HTTPS proxy on `127.0.0.1:20136`. |
| `migrate-sessions.py` | Manage the free list and move desktop-app chats to or off free models. |
| `restart-gateway.ps1` | Stops every running copy of both services and starts exactly one of each. |
| `launch-hidden.vbs` | Starts OmniRoute and both services with no window; for a login task. |
| `autostart.ps1`, `start-gateway.ps1` | Start the services from PowerShell. |
| `claude-free.ps1`, `free-resume.py`, `free-resume.ps1` | Open one terminal session, or resume a chosen chat, on free models. |
| `claude-failover.ps1` | Older per-folder base-URL routing for terminal sessions. |

## How the Claude gateway decides

| Situation | Result |
|---|---|
| You picked **Opus 4.6** (mapped to `free`) | Free models, in any chat. Work-folder chats get a warning note. Claude answers if the free chain fails. |
| Claude limit reached, chat **on the free list** | Free models until the limit resets, then back to Claude. |
| Claude limit reached, chat **not on the list** | Anthropic's normal limit message. Switch the chat to Opus 4.6 to continue. |
| Chat in a folder listed in `never_free_dirs` | Never moved automatically. |

The first reply after a switch starts with a one-line note saying which backend answered. Your Anthropic credentials are never sent to the free providers.

Free models need some repairs to work with Claude Code, which the gateway does:

- removes Anthropic-signed thinking blocks and Claude-only request fields;
- refuses Claude Code's *message threads* (requests that rely on tools and history stored at Anthropic) with `thread_unsupported_request`, so Claude Code resends complete requests;
- converts tool calls that a model wrote as text (`<tool_call><function=...>`, `<tool_call>{json}`, `<invoke name="...">`) into real tool calls, and cleans earlier ones out of the history. Replies that may contain tool calls are therefore delivered whole rather than streamed.

## Setup

1. Install OmniRoute, create a combo named `free-chain` with your free models, and set `OMNIROUTE_API_KEY` (and optionally `OPENROUTER_API_KEY`) as user environment variables.
2. Copy `skills/model-failover` to `~/.claude/skills/model-failover`.
3. Create a local certificate for the HTTPS proxy. It is trusted only by Claude Code, not by Windows:
   ```bash
   mkdir -p ~/.model-failover/tls && cd ~/.model-failover/tls
   openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout api-anthropic.key -out api-anthropic.pem \
     -subj "/CN=api.anthropic.com" -addext "subjectAltName=DNS:api.anthropic.com" \
     -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature,keyEncipherment"
   ```
4. Add to `~/.claude/settings.json`:
   ```json
   "env": {
     "HTTPS_PROXY": "http://127.0.0.1:20136",
     "NODE_EXTRA_CA_CERTS": "C:\\Users\\<you>\\.model-failover\\tls\\api-anthropic.pem"
   },
   "modelOverrides": { "claude-opus-4-6": "free" }
   ```
5. Create `~/.model-failover/claude-gateway.json` and list your work folders (substrings of the folder path):
   ```json
   { "never_free_dirs": ["my-work-repo"], "free_sessions": [], "fallback_model": "claude-opus-5-5" }
   ```
6. Start the services: `powershell -ExecutionPolicy Bypass -File restart-gateway.ps1`. To start them at login, point a scheduled task at `wscript.exe //B launch-hidden.vbs`, optionally repeating every 10 minutes as a watchdog.
7. Quit the Claude desktop app, then add chats to the free list:
   ```bash
   python migrate-sessions.py --free-set "my chat title" --apply
   ```

## Things to know

- **The gateway must be running.** Every Claude Code chat goes through its proxy, so if it is down Claude Code cannot connect. Restart it, or remove `HTTPS_PROXY` from `settings.json`.
- `migrate-sessions.py` edits the desktop app's own chat files. That is unsupported by the app, so it refuses to run while the app is open and records every change for `--undo`.
- Free models have smaller context windows and weaker tool use than Claude. Compact long chats, and expect lower quality.
- Moving a chat between Claude and free models discards Claude's prompt cache for it, so the next Claude turn costs more.
- Regular claude.ai chats (the Chat tab) run on Anthropic's servers and cannot be routed.
- Anthropic's real limit reply has only been simulated in testing; detection uses its reset headers and limit wording.
- Free quotas reset daily or by each provider's own rule, not monthly. Using login-based routes inside OmniRoute may breach those services' terms.
- Both services bind to `127.0.0.1` only. Do not expose them.

## License

MIT. See [LICENSE](LICENSE).
