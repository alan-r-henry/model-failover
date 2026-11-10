#!/usr/bin/env python3
"""claude-gateway: a local Anthropic-API pass-through that sits in front of Claude Code.

Normal traffic goes to api.anthropic.com untouched (your own login headers are forwarded
as-is, to Anthropic only). When Anthropic answers that a usage limit is reached (the 5-hour
or weekly limit, HTTP 429 with a long reset time, or 402), the gateway re-sends that same
request to OmniRoute's free combo and keeps using it until the limit's reset time, then goes
back to Claude by itself.

Your Anthropic credentials are NEVER sent to the free tier, and are never logged.

  python claude-gateway.py serve [--port 20135]
  python claude-gateway.py status

Point Claude Code at it with ANTHROPIC_BASE_URL=http://127.0.0.1:20135
"""
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class ExclusiveServer(ThreadingHTTPServer):
    """Refuses a port another process already holds. Python's default SO_REUSEADDR lets a second copy
    share the port on Windows, and requests then go to whichever copy (possibly old code) Windows picks."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


PORT = 20135
HOME = os.path.join(os.path.expanduser("~"), ".model-failover")
LOG = os.environ.get("CLAUDE_GW_LOG") or os.path.join(HOME, "claude-gateway.log")
UPSTREAM = os.environ.get("CLAUDE_GW_UPSTREAM", "https://api.anthropic.com")
FREE_BASE = os.environ.get("CLAUDE_GW_FREE_BASE", "http://localhost:20128")
FREE_MODEL = os.environ.get("CLAUDE_GW_FREE_MODEL", "free-chain")
FREE_KEY_ENV = "OMNIROUTE_API_KEY"
FREE_ALIASES = {"free", "free-chain", "free-model", "free-auto"}   # model names that mean "use the free chain"

MIN_LONG_LIMIT_S = int(os.environ.get("CLAUDE_GW_MIN_LONG", "120"))  # shorter 429s are burst limits: pass through
MIN_REST_S = int(os.environ.get("CLAUDE_GW_MIN_REST", "60"))
DEFAULT_LIMIT_S = 3600       # limit text seen but no reset time given
MAX_REST_S = 8 * 24 * 3600   # weekly limits can be days
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}

_lock = threading.Lock()
_state = {"limited_until": 0.0, "reason": "", "to_free": 0, "to_claude": 0}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    # Only echo to a real console. If stdout is a pipe nobody drains (a launcher, a test harness), a
    # blocking print would freeze every request thread once the pipe buffer fills.
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            print(line, flush=True)
    except (OSError, ValueError):
        pass


def user_env(name):
    v = os.environ.get(name)
    if v:
        return v
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            pass
    return ""


def limit_seconds(status, headers, body):
    """Seconds Claude is unavailable if this response is a usage-limit refusal, else 0."""
    if status not in (429, 402):
        return 0
    secs = None
    for h in ("anthropic-ratelimit-unified-reset", "x-ratelimit-reset"):
        v = headers.get(h)
        if v and v.strip().replace(".", "", 1).isdigit():
            t = float(v)
            secs = t - time.time() if t > 1e9 else t        # epoch seconds, or a delta
            break
    if secs is None:
        ra = headers.get("retry-after")
        if ra and ra.strip().isdigit():
            secs = int(ra)
    text = (body or "").lower()
    looks_like_limit = (headers.get("anthropic-ratelimit-unified-status", "").lower() == "rejected"
                        or any(s in text for s in ("usage limit", "limit reached", "5-hour", "weekly limit",
                                                   "credit balance", "billing")))
    if secs is None:
        return DEFAULT_LIMIT_S if (looks_like_limit or status == 402) else 0
    if status == 402 or looks_like_limit or secs >= MIN_LONG_LIMIT_S:
        return int(min(max(secs, MIN_REST_S), MAX_REST_S))
    return 0


def strip_for_free(payload):
    """Free models cannot use Anthropic-signed thinking blocks from earlier turns."""
    p = json.loads(json.dumps(payload))
    p["model"] = FREE_MODEL
    for k in ("thinking", "context_management", "output_config"):
        p.pop(k, None)
    tools = {t.get("name"): t.get("input_schema") or {} for t in p.get("tools") or [] if isinstance(t, dict)}
    for m in p.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            m["content"] = [b for b in c if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))] or [{"type": "text", "text": "(continued)"}]
        if m.get("role") == "assistant" and tools:
            _scrub_text_tool_calls(m, tools)
    return p


def _scrub_text_tool_calls(m, tools):
    """Earlier assistant turns that wrote tool calls as text teach the model to keep doing it: replace them."""
    note = "[A tool call written as plain text here was never run. Call tools with the tool-calling interface.]"
    c = m.get("content")
    if isinstance(c, str):
        hit = parse_text_tool_calls(c, tools)
        if hit:
            m["content"] = (hit[0] + "\n" + note).strip()
        return
    for b in c or []:
        if isinstance(b, dict) and b.get("type") == "text":
            hit = parse_text_tool_calls(b.get("text", ""), tools)
            if hit:
                b["text"] = (hit[0] + "\n" + note).strip()


# --- tool calls written as text ---------------------------------------------------------
# Some free models (Qwen/Hermes style) put tool calls in the reply TEXT, e.g.
#   <tool_call><function=Bash><parameter=command>ls</parameter></function></tool_call>
# or <tool_call>{"name": "Bash", "arguments": {...}}</tool_call>. Claude Code would just print that.
# The free reply is buffered and such text is turned into real tool_use blocks.
_FUNC_RE = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|$)", re.S)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)
_JSON_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)
# Anthropic-XML style, inside any wrapper (<function_calls>, <dots_function_call>, ...):
#   <invoke name="PowerShell"><parameter name="command">ls</parameter></invoke>
_INVOKE_RE = re.compile(r"<(?:[\w-]+:)?invoke\s+name=\"([^\"]+)\"\s*>(.*?)(?:</(?:[\w-]+:)?invoke>|$)", re.S)
_NPARAM_RE = re.compile(r"<(?:[\w-]+:)?parameter\s+name=\"([^\"]+)\"\s*>\n?(.*?)\n?</(?:[\w-]+:)?parameter>", re.S)
_WRAPPER_TAIL_RE = re.compile(r"<[\w:-]*(?:function|tool)[\w:-]*>\s*$", re.I)


def _coerce(v, schema):
    if (schema or {}).get("type") in ("integer", "number", "boolean", "array", "object"):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def parse_text_tool_calls(text, tools):
    """(text before the calls, [(name, input)]) if text holds tool calls for known tools, else None."""
    inv = _INVOKE_RE.search(text)
    if inv:
        calls = []
        for m in _INVOKE_RE.finditer(text, inv.start()):
            if m.group(1) not in tools:
                return None
            props = tools[m.group(1)].get("properties") or {}
            calls.append((m.group(1), {k: _coerce(v, props.get(k)) for k, v in _NPARAM_RE.findall(m.group(2))}))
        return _WRAPPER_TAIL_RE.sub("", text[:inv.start()]).rstrip(), calls
    starts = [i for i in (text.find("<tool_call>"), text.find("<function=")) if i >= 0]
    if not starts:
        return None
    i, calls = min(starts), []
    for m in _FUNC_RE.finditer(text, i):
        if m.group(1) not in tools:
            return None
        props = tools[m.group(1)].get("properties") or {}
        calls.append((m.group(1), {k: _coerce(v, props.get(k)) for k, v in _PARAM_RE.findall(m.group(2))}))
    if not calls:
        for m in _JSON_CALL_RE.finditer(text, i):
            try:
                obj = json.loads(m.group(1))
                args = obj.get("arguments", obj.get("input", obj.get("parameters", {})))
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                return None
            if obj.get("name") not in tools or not isinstance(args, dict):
                return None
            calls.append((obj["name"], args))
    return (text[:i].rstrip(), calls) if calls else None


def repair_message(msg, tools):
    """Turn text-written tool calls in an Anthropic message into tool_use blocks. True if changed."""
    out, changed = [], False
    for b in msg.get("content") or []:
        hit = parse_text_tool_calls(b.get("text", ""), tools) if b.get("type") == "text" else None
        if not hit:
            out.append(b)
            continue
        changed = True
        if hit[0]:
            out.append({"type": "text", "text": hit[0]})
        for name, inp in hit[1]:
            out.append({"type": "tool_use", "id": "toolu_free_%s" % os.urandom(8).hex(), "name": name, "input": inp})
    if changed:
        msg["content"] = out
        msg["stop_reason"] = "tool_use"
    return changed


def sse_to_message(raw):
    msg, blocks, tail = None, {}, {}
    for part in re.split(r"(?:\r?\n){2}", raw.decode("utf-8", "replace")):
        data = next((l[5:].strip() for l in part.splitlines() if l.startswith("data:")), None)
        if not data:
            continue
        try:
            ev = json.loads(data)
        except ValueError:
            continue                                  # e.g. "data: [DONE]" from an OpenAI-style relay
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "message_start":
            msg = ev["message"]
        elif t == "content_block_start":
            b = dict(ev["content_block"])
            if b.get("type") == "tool_use":
                b["_json"] = ""
            blocks[ev["index"]] = b
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            b = blocks.setdefault(ev.get("index", 0), {"type": "tool_use", "_json": ""} if dt == "input_json_delta"
                                  else {"type": "thinking"} if dt in ("thinking_delta", "signature_delta")
                                  else {"type": "text", "text": ""})
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = d.get("signature", "")
            elif dt == "input_json_delta":
                b["_json"] += d.get("partial_json", "")
        elif t == "message_delta":
            tail = ev
        elif t == "error":
            return None
    if msg is None:
        return None
    for b in blocks.values():
        if "_json" in b:
            js = b.pop("_json")
            try:
                b["input"] = json.loads(js) if js else b.get("input") or {}
            except ValueError:
                return None
    msg["content"] = [blocks[k] for k in sorted(blocks)]
    msg["stop_reason"] = (tail.get("delta") or {}).get("stop_reason") or msg.get("stop_reason")
    msg["_usage_tail"] = tail.get("usage")
    return msg


def message_to_sse(msg):
    def ev(name, obj):
        return "event: %s\ndata: %s\n\n" % (name, json.dumps(obj))
    usage_tail = msg.pop("_usage_tail", None) or {"output_tokens": 0}
    content = msg.get("content") or []
    start = dict(msg, content=[], stop_reason=None)
    out = [ev("message_start", {"type": "message_start", "message": start})]
    for i, b in enumerate(content):
        t = b.get("type")
        if t == "text":
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b.get("text", "")}}))
        elif t == "thinking":
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": ""}}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b.get("thinking", "")}}))
            if b.get("signature"):
                out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}}))
        elif t == "tool_use":
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b.get("input") or {})}}))
        else:
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": b}))
        out.append(ev("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": msg.get("stop_reason"), "stop_sequence": None}, "usage": usage_tail}))
    out.append(ev("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


def repair_free_body(raw, content_type, tools):
    """Body with text-written tool calls fixed, or None if there was nothing to fix (or it can't be parsed)."""
    if not tools:
        return None
    if "text/event-stream" in content_type:
        msg = sse_to_message(raw)
        return message_to_sse(msg) if msg and repair_message(msg, tools) else None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return json.dumps(msg).encode() if isinstance(msg, dict) and repair_message(msg, tools) else None


class _Buffered:
    """Response-like wrapper around an already-read body, for Handler._stream."""

    def __init__(self, resp, body):
        self.status, self.headers, self._io = resp.status, resp.headers, __import__("io").BytesIO(body)

    def getheaders(self):
        return self.headers.items()

    def read1(self, n):
        return self._io.read(n)


# --- work-group safety rule -------------------------------------------------------------
# Requests from these project folders are NEVER sent to a free model: if Claude is limited
# they get Anthropic's own limit reply, unchanged. Matching is on the working-directory lines
# Claude Code puts in every system prompt (not on the whole body, because a CLAUDE.md that
# merely names these projects appears in every session). A request with no such line inherits
# its session's earlier verdict; if the session is unknown it is treated as protected.
CONFIG = os.path.join(HOME, "claude-gateway.json")
DEFAULT_CONFIG = {"never_free_dirs": []}   # list your work folders in ~/.model-failover/claude-gateway.json
_sessions = {}   # session id -> "ok" | "work"


def never_free_dirs():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return [s.lower() for s in json.load(f)["never_free_dirs"]]
    except (OSError, ValueError, KeyError):
        try:
            os.makedirs(HOME, exist_ok=True)
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except OSError:
            pass
        return [s.lower() for s in DEFAULT_CONFIG["never_free_dirs"]]


def free_sessions():
    """Opt-in list: Claude Code session ids the user chose to let use free models. Every other chat stays on Claude."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return [s.lower() for s in json.load(f).get("free_sessions", []) if s]
    except (OSError, ValueError):
        return []


def _text_of(payload):
    """System prompt plus the text blocks of every user turn: in long or compacted chats the environment
    block arrives in a later user turn. Tool results are excluded, so quoted output cannot fake a folder."""
    s = payload.get("system")
    out = []
    if isinstance(s, str):
        out.append(s)
    elif isinstance(s, list):
        out += [b.get("text", "") for b in s if isinstance(b, dict)]
    for m in payload.get("messages") or []:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(out)


def classify(payload):
    """'ok' (may use free), or 'work' / 'not-listed' (must not). Opt-in: only sessions on the free list are 'ok',
    and a work folder blocks free even for a listed session."""
    sid = str((payload.get("metadata") or {}).get("user_id", ""))
    folder = _folder_verdict(payload, sid)
    if folder == "work":
        return "work"
    return "ok" if sid and any(s in sid.lower() for s in free_sessions()) else "not-listed"


def _folder_verdict(payload, sid):
    text = _text_of(payload)
    verdict = None
    for m in re.finditer(r"(?i)(primary working directory|additional working directories?)\s*:", text):
        window = text[m.start(): m.start() + 700].lower()
        window = window.split("platform:")[0]          # stop at the end of the environment's folder lines
        verdict = "ok" if verdict is None else verdict
        if any(p in window for p in never_free_dirs()):
            verdict = "work"
    if verdict is not None and sid:
        _sessions[sid] = verdict if _sessions.get(sid) != "work" else "work"
        if len(_sessions) > 2000:
            _sessions.pop(next(iter(_sessions)))
    if verdict is None:
        verdict = _sessions.get(sid, "unknown") if sid else "unknown"
    return verdict


class ClientGone(Exception):
    pass


FREE_COOLDOWN_S = int(os.environ.get("CLAUDE_GW_FREE_COOLDOWN", "300"))


def fallback_model():
    """Claude model that answers 'free' requests when the free chain is down (claude-gateway.json: fallback_model)."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f).get("fallback_model") or "claude-opus-4-6"
    except (OSError, ValueError):
        return "claude-opus-4-6"


def _mark_free_down(code):
    """The free chain failed before replying. Skip it for a few minutes (quota or outage), except for HTTP 400,
    which is usually this one chat being too large for a free model: other chats can still use it."""
    if code == 400:
        return
    _state["free_down_until"] = time.time() + FREE_COOLDOWN_S
    log("free chain unavailable (HTTP %s): using Claude for %ds, then trying free again" % (code or "unreachable", FREE_COOLDOWN_S))


# --- in-chat status notices ---------------------------------------------------------------
# When the backend serving a chat changes (free <-> Claude), say so at the top of the reply, once per change.
_last_backend = {}          # session id -> label of the backend that served its previous request


def _pretty_model(name):
    p = name.replace("claude-", "").split("-")
    return (p[0].capitalize() + (" " + ".".join(p[1:]) if len(p) > 1 else "")) if p and p[0] else name


def notice_for(sid, label, text):
    """text if this session's backend changed to `label` since its previous request, else None."""
    if not sid:
        return None
    with _lock:
        prev = _last_backend.get(sid)
        _last_backend[sid] = label
        if len(_last_backend) > 2000:
            _last_backend.pop(next(iter(_last_backend)))
    return text if prev != label else None


def claude_back_notice(sid):
    """After an automatic move to free (Claude usage limit): tell the session when Claude serves it again."""
    with _lock:
        if sid and _last_backend.get(sid) == "free-limit":
            _last_backend[sid] = "claude"
            return "ℹ Claude is available again. This chat is back on Claude.\n\n"
    return None


NOTE_FREE = "ℹ Free model in use (NVIDIA / OpenRouter free tier), not Claude.\n\n"
NOTE_FREE_WORK = ("ℹ Free model in use (NVIDIA / OpenRouter free tier), not Claude. This is a work chat: "
                  "its conversation and any files read are sent to those providers.\n\n")
NOTE_PROTECTED = "ℹ Work project: free models are never used here. Answered by Claude %s (uses your Claude usage).\n\n"
NOTE_NOT_LISTED = ("ℹ This chat isn't on your free list, so free models were not used. "
                   "Answered by Claude %s (uses your Claude usage).\n\n")
NOTE_FALLBACK = ("ℹ Free models are unavailable right now (quota used up or outage). Switched to Claude %s, "
                 "which uses your Claude usage. Free is retried automatically in a few minutes.\n\n")


def note_limit_free():
    left = max(0, int(_state["limited_until"] - time.time()))
    return ("ℹ Claude usage limit reached. Using free models until it resets (about %dh %02dm). "
            "Work projects are never sent to free.\n\n" % (left // 3600, left % 3600 // 60))


class NoticeInjector:
    """Rewrites an Anthropic SSE stream to carry one extra text block, placed after any thinking blocks
    (a reply with tool calls must still start with its thinking block). Later block indexes shift by one."""

    def __init__(self, text):
        self.text, self.buf, self.injected, self.shift, self.seen = text, b"", False, 0, 0

    def _block(self, idx):
        def ev(name, obj):
            return ("event: %s\ndata: %s\n\n" % (name, json.dumps(obj))).encode()
        return (ev("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
                + ev("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": self.text}})
                + ev("content_block_stop", {"type": "content_block_stop", "index": idx}))

    def _event(self, raw):
        lines = raw.replace(b"\r\n", b"\n").split(b"\n")
        di = next((i for i, l in enumerate(lines) if l.startswith(b"data:")), None)
        if di is None:
            return raw + b"\n\n"
        try:
            obj = json.loads(lines[di][5:].strip())
        except ValueError:
            return raw + b"\n\n"
        t, pre = obj.get("type"), b""
        if t == "content_block_start":
            self.seen = max(self.seen, obj["index"] + 1)
            if not self.injected and (obj.get("content_block") or {}).get("type") not in ("thinking", "redacted_thinking"):
                pre, self.injected, self.shift = self._block(obj["index"]), True, 1
        elif t == "message_delta" and not self.injected:
            pre, self.injected = self._block(self.seen), True
        if self.shift and t in ("content_block_start", "content_block_delta", "content_block_stop") and "index" in obj:
            obj["index"] += self.shift
            lines[di] = b"data: " + json.dumps(obj).encode()
            return pre + b"\n".join(lines) + b"\n\n"
        return pre + raw + b"\n\n"

    def feed(self, data, final=False):
        self.buf += data
        parts = re.split(rb"(?:\r?\n){2}", self.buf)
        self.buf = b"" if final else parts.pop()
        return b"".join(self._event(p) for p in parts if p.strip())


# Our own outgoing calls must never go back through our own proxy listener.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-gateway/1.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *a):
        if DEBUG:
            log("http: " + (fmt % a))

    def setup(self):
        super().setup()
        # On the TLS-intercepted connection (desktop app) speak real HTTP/1.1: every reply has an explicit
        # Content-Length or chunked framing, so the client never has to infer "end of body" from a closed
        # socket (it does not, and its connection pool then fills up and stalls). The plain base-URL
        # listener keeps the simple HTTP/1.0 style that terminal Claude Code already works with.
        self._tls = isinstance(self.request, ssl.SSLSocket)
        if self._tls:
            self.protocol_version = "HTTP/1.1"

    def _to_client(self, fn, *a):
        try:
            return fn(*a)
        except OSError as e:
            raise ClientGone(str(e))

    def _write_chunk(self, chunk):
        if self._tls:
            chunk = b"%x\r\n" % len(chunk) + chunk + b"\r\n"
        self._to_client(self.wfile.write, chunk)
        self._to_client(self.wfile.flush)

    def _send_body(self, code, headers, body):
        self.send_response(code)
        for k, v in headers:
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self._to_client(self.end_headers)
        self._to_client(self.wfile.write, body)

    def _stream(self, resp, code, extra=(), notice=None):
        """Relay a response. notice: a one-line status message shown at the top of the assistant's reply
        (streamed replies only); it is inserted as an extra text block, never into the model's own text."""
        self.send_response(code)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        if self._tls:
            self.send_header("Transfer-Encoding", "chunked")
        self._to_client(self.end_headers)
        inj = NoticeInjector(notice) if notice and "text/event-stream" in (resp.headers.get("Content-Type") or "") else None
        read = resp.read1 if hasattr(resp, "read1") else resp.read
        while True:
            chunk = read(8192)
            if not chunk:
                if inj:
                    chunk = inj.feed(b"", final=True)
                    if chunk:
                        self._write_chunk(chunk)
                break
            if inj:
                chunk = inj.feed(chunk)
                if not chunk:
                    continue
            self._write_chunk(chunk)
        if self._tls:
            self._to_client(self.wfile.write, b"0\r\n\r\n")
            self._to_client(self.wfile.flush)

    def do_GET(self):
        if self.path.split("?")[0] == "/__status":
            now = time.time()
            lim = _state["limited_until"] > now
            data = json.dumps({"serving": "free-chain" if lim else "claude",
                               "limited_for_s": int(_state["limited_until"] - now) if lim else 0,
                               "reason": _state["reason"] if lim else "",
                               "to_free": _state["to_free"], "blocked": _state.get("blocked", 0),
                               "claude_fallbacks": _state.get("to_claude_fallback", 0),
                               "free_down_for_s": max(0, int(_state.get("free_down_until", 0) - now)),
                               "protected": never_free_dirs()}).encode()
            return self._send_body(200, [("Content-Type", "application/json")], data)
        self._proxy()

    def do_POST(self):
        self._proxy()

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_POST

    def _proxy(self):
        if DEBUG:
            log("headers: %s %s len=%s expect=%s te=%s" % (self.command, self.path.split("?")[0],
                self.headers.get("Content-Length"), self.headers.get("Expect"), self.headers.get("Transfer-Encoding")))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = self.path
        if DEBUG:
            log("request: %s %s (%d bytes) via %s" % (self.command, path.split("?")[0], len(body),
                                                      "tls-proxy" if isinstance(self.connection, ssl.SSLSocket) else "base-url"))
        if path.startswith("/free/"):
            return self._forced_free(path[5:], body)
        # Chosen in the model picker: a model named "free" (or free-chain / free-model) always goes to the
        # free chain, limit or not. Same work-group rule as everything else: protected/unknown -> refused.
        route = path.split("?")[0]
        if self.command == "POST" and route in ("/v1/messages", "/v1/messages/count_tokens"):
            try:
                want = str(json.loads(body or b"{}").get("model", "")).lower()
            except ValueError:
                want = ""
            if want in FREE_ALIASES or want.startswith("free-"):
                return self._picker_free(route, body)
        is_msg = self.command == "POST" and path.split("?")[0] == "/v1/messages"
        try:
            verdict, sid = "ok", ""
            if is_msg:
                try:
                    pl = json.loads(body or b"{}")
                    verdict = classify(pl)
                    sid = str((pl.get("metadata") or {}).get("user_id", ""))
                except ValueError:
                    verdict = "unknown"
            may_use_free = verdict == "ok"
            limit_note = lambda: notice_for(sid, "free-limit", note_limit_free())
            if is_msg and may_use_free and time.time() < _state["limited_until"]:
                return self._free(body, notice=limit_note)
            resp, err = self._claude(body)
            if err is not None:
                code, hdrs, ebody = err
                secs = limit_seconds(code, {k.lower(): v for k, v in hdrs}, ebody.decode("utf-8", "replace"))
                if is_msg and secs > 0:
                    with _lock:       # the limit is account-wide, so remember it either way
                        _state["limited_until"] = time.time() + secs
                        _state["reason"] = "Claude limit (HTTP %d)" % code
                    if may_use_free:
                        log("Claude usage limit reached (HTTP %d), using free-chain for %ds" % (code, secs))
                        return self._free(body, notice=limit_note)
                    _state["blocked"] = _state.get("blocked", 0) + 1
                    log("Claude usage limit reached; request from a protected/unknown session (%s) NOT sent to free tier" % verdict)
                return self._send_body(code, hdrs, ebody)      # any other error: untouched
            return self._stream(resp, resp.status, notice=claude_back_notice(sid) if is_msg else None)
        except ClientGone:
            return
        except (OSError, http.client.HTTPException) as e:
            log("proxy error: %s" % e)
            try:
                self._send_body(502, [("Content-Type", "application/json")],
                                json.dumps({"type": "error", "error": {"type": "api_error",
                                            "message": "claude-gateway: " + str(e)}}).encode())
            except ClientGone:
                pass

    def _forced_free(self, path, body):
        """ANTHROPIC_BASE_URL=http://127.0.0.1:20135/free : always the free chain (used by claude-free).
        Same work-group safety rule as automatic failover."""
        def err(code, typ, msg):
            return self._send_body(code, [("Content-Type", "application/json")], json.dumps(
                {"type": "error", "error": {"type": typ, "message": msg}}).encode())
        try:
            route = path.split("?")[0]
            if self.command == "POST" and route == "/v1/messages":
                verdict = classify(json.loads(body or b"{}"))
                if verdict != "ok":
                    _state["blocked"] = _state.get("blocked", 0) + 1
                    log("free mode refused: protected/unknown session (%s)" % verdict)
                    return err(403, "permission_error", "Free mode is disabled for protected work-group projects.")
                return self._free(body)
            if self.command == "POST" and route == "/v1/messages/count_tokens":
                return self._send_body(200, [("Content-Type", "application/json")],
                                       json.dumps({"input_tokens": max(1, len(body) // 4)}).encode())
            return err(404, "not_found_error", "free mode only serves /v1/messages")
        except ClientGone:
            return
        except ValueError:
            return err(400, "invalid_request_error", "bad json")

    def _claude(self, body):
        """Forward to Anthropic with the caller's own headers. Returns (resp, None) or (None, err)."""
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        req = urllib.request.Request(UPSTREAM.rstrip("/") + self.path, data=body or None,
                                     headers=hdrs, method=self.command)
        try:
            if UPSTREAM.startswith("https"):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                     urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            else:
                opener = _NO_PROXY_OPENER
            return opener.open(req, timeout=600), None
        except urllib.error.HTTPError as e:
            return None, (e.code, list(e.headers.items()), e.read())

    def _free(self, body, soft=False, notice=None):
        """Send to the free chain. soft=True: if it cannot be served BEFORE any reply bytes go out
        (no key, HTTP error, unreachable), mark the chain down for a while and return False so the caller
        can fall back to Claude. Returns True once the free chain is answering."""
        def fail(code, msg):
            if soft:
                return False
            return self._send_body(code, [("Content-Type", "application/json")], json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": msg}}).encode())
        key = user_env(FREE_KEY_ENV)
        if not key:
            return fail(503, "Claude limit reached and OMNIROUTE_API_KEY is not set")
        try:
            payload = strip_for_free(json.loads(body or b"{}"))
        except ValueError:
            return fail(400, "bad json")
        if "thread" in payload:
            # Message threads keep tools and earlier turns on Anthropic's server, which the free chain does not have.
            # This error makes Claude Code resend the turn in full and stay stateless on this model for the session.
            log("free request used a message thread: told the client to resend it without one")
            self._send_body(400, [("Content-Type", "application/json")], json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "thread_unsupported_request: the free tier does not support message threads",
                "details": {"error_code": "thread_unsupported_request"}}}).encode())
            return True
        # Fresh headers only: the caller's Anthropic credentials must not reach the free tier.
        req = urllib.request.Request(FREE_BASE.rstrip("/") + "/v1/messages", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "x-api-key": key,
                                              "Authorization": "Bearer " + key,
                                              "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")},
                                     method="POST")
        try:
            resp = _NO_PROXY_OPENER.open(req, timeout=600)
        except urllib.error.HTTPError as e:
            log("free tier error HTTP %d" % e.code)
            if soft:
                _mark_free_down(e.code)
                return False
            return self._send_body(e.code, list(e.headers.items()), e.read())
        except (OSError, http.client.HTTPException) as e:
            log("free tier unreachable: %s" % e)
            if soft:
                _mark_free_down(0)
                return False
            raise
        _state["to_free"] += 1
        tools = {t.get("name"): t.get("input_schema") or {} for t in payload.get("tools") or [] if isinstance(t, dict)}
        if not tools or resp.status != 200:
            log("free reply relayed without tool-call check (tools=%d, status=%s)" % (len(tools), resp.status))
        if tools and resp.status == 200:
            raw = resp.read()
            fixed = repair_free_body(raw, resp.headers.get("Content-Type") or "", tools)
            if fixed is not None:
                log("free reply had tool calls written as text: converted to real tool calls")
            elif b"<tool_call" in raw or b"<function=" in raw or b"<invoke " in raw:
                dump = os.path.join(HOME, "last-unrepaired-free-reply.txt")
                with open(dump, "wb") as f:
                    f.write(raw)
                log("free reply looks like a text tool call but could not be converted; saved to %s" % dump)
            resp = _Buffered(resp, fixed if fixed is not None else raw)
        self._stream(resp, resp.status, [("X-Served-By", "free-chain")], notice=notice() if notice else None)
        return True

    def _claude_fallback(self, payload, why, notice=None):
        """A 'free' request that the free chain cannot or must not serve: answer it with real Claude.
        The model name 'free' is swapped for the configured Claude model; the caller's own login is used."""
        p = dict(payload)
        p["model"] = fallback_model()
        _state["to_claude_fallback"] = _state.get("to_claude_fallback", 0) + 1
        log("free request answered by Claude (%s) with %s" % (why, p["model"]))
        resp, err = self._claude(json.dumps(p).encode())
        if err is not None:
            code, hdrs, ebody = err
            secs = limit_seconds(code, {k.lower(): v for k, v in hdrs}, ebody.decode("utf-8", "replace"))
            if secs > 0:
                with _lock:
                    _state["limited_until"] = time.time() + secs
                    _state["reason"] = "Claude limit (HTTP %d)" % code
            return self._send_body(code, hdrs, ebody)
        return self._stream(resp, resp.status, [("X-Served-By", "claude-fallback")], notice=notice)

    def _picker_free(self, route, body):
        """Model 'free' (Opus 4.6 in the picker) chosen explicitly: that choice is the opt-in, so it goes to the free
        chain in ANY chat, work folders and unlisted chats included. Real Claude only if the free chain fails."""
        try:
            if route == "/v1/messages/count_tokens":
                return self._send_body(200, [("Content-Type", "application/json")],
                                       json.dumps({"input_tokens": max(1, len(body) // 4)}).encode())
            payload = json.loads(body or b"{}")
            work = classify(payload) == "work"
            sid = str((payload.get("metadata") or {}).get("user_id", ""))
            pretty = _pretty_model(fallback_model())
            if work:
                log("free model chosen explicitly in a work-folder chat: sending to free tier")
            if time.time() < _state.get("free_down_until", 0):
                return self._claude_fallback(payload, "free chain recently failed",
                                             notice_for(sid, "fb-fail", NOTE_FALLBACK % pretty))
            note = NOTE_FREE_WORK if work else NOTE_FREE
            if self._free(body, soft=True, notice=lambda: notice_for(sid, "free", note)):
                return
            return self._claude_fallback(payload, "free chain failed", notice_for(sid, "fb-fail", NOTE_FALLBACK % pretty))
        except ClientGone:
            return
        except ValueError:
            return self._send_body(400, [("Content-Type", "application/json")], b'{"type":"error"}')
        except (OSError, http.client.HTTPException) as e:
            log("proxy error: %s" % e)
            try:
                self._send_body(502, [("Content-Type", "application/json")],
                                json.dumps({"type": "error", "error": {"type": "api_error",
                                            "message": "claude-gateway: " + str(e)}}).encode())
            except ClientGone:
                pass


# --- forward-proxy mode (for the Claude desktop app) --------------------------------------
# The desktop app forces ANTHROPIC_BASE_URL, so a base-URL gateway cannot sit in front of its
# Code-tab sessions. It does honour the standard HTTPS_PROXY / NODE_EXTRA_CA_CERTS variables
# (set in the app's local environment editor, so only for local Code sessions). This listener
# is that proxy: every CONNECT is tunnelled byte-for-byte untouched EXCEPT api.anthropic.com,
# whose TLS is terminated here with a certificate valid ONLY for that one name (a self-signed
# leaf; the sessions are told to trust exactly that file, nothing system-wide). The decrypted
# request then goes through the normal Handler: forwarded to Anthropic with its own headers,
# moved to the free chain on a usage limit, never for work-group projects.
DEBUG = bool(os.environ.get("CLAUDE_GW_DEBUG"))
PROXY_PORT = int(os.environ.get("CLAUDE_GW_PROXY_PORT", "20136"))
TLS_DIR = os.path.join(HOME, "tls")
CERT_FILE = os.path.join(TLS_DIR, "api-anthropic.pem")
KEY_FILE = os.path.join(TLS_DIR, "api-anthropic.key")
INTERCEPT_HOSTS = {"api.anthropic.com"}
_tls_ctx = None


def tls_context():
    global _tls_ctx
    if _tls_ctx is None and os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        ctx.set_alpn_protocols(["http/1.1"])
        _tls_ctx = ctx
    return _tls_ctx


def _pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "claude-gateway-proxy/1.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *a):
        if DEBUG:
            log("proxy-http: " + (fmt % a))

    def do_CONNECT(self):
        self.close_connection = True
        if DEBUG:
            log("proxy: CONNECT %s from %s" % (self.path, self.client_address[1]))
        host, _, port = self.path.rpartition(":")
        host = host.strip("[]")
        port = int(port) if port.isdigit() else 443
        if host.lower() in INTERCEPT_HOSTS and port == 443:
            ctx = tls_context()
            if ctx is None:
                log("CONNECT %s refused: no certificate at %s" % (host, CERT_FILE))
                return self.send_error(502, "claude-gateway: certificate not installed")
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            try:
                tls = ctx.wrap_socket(self.connection, server_side=True)
            except (ssl.SSLError, OSError) as e:
                log("client rejected the api.anthropic.com certificate (%s): is NODE_EXTRA_CA_CERTS set?" % e)
                return
            if DEBUG:
                log("proxy: TLS handshake ok (%s, alpn=%s)" % (tls.version(), tls.selected_alpn_protocol()))
            try:
                tls.settimeout(600)
                Handler(tls, self.client_address, self.server)     # normal failover logic on the decrypted request
            except (OSError, ssl.SSLError) as e:
                if DEBUG:
                    log("proxy: connection ended: %s" % e)
            except Exception as e:                                 # never let one bad request kill the listener
                log("proxy: unexpected %s: %s" % (type(e).__name__, e))
            finally:
                # Clean TLS shutdown (close_notify): a response that ends by closing the connection
                # (HTTP/1.0 style) looks truncated to strict clients unless the TLS goodbye is sent.
                try:
                    tls.settimeout(3)
                    tls.unwrap()
                except (OSError, ssl.SSLError, ValueError):
                    pass
                try:
                    tls.close()
                except OSError:
                    pass
            return
        try:
            up = socket.create_connection((host, port), timeout=20)
        except OSError as e:
            return self.send_error(502, "cannot reach %s:%d (%s)" % (host, port, e))
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        up.settimeout(None)
        t = threading.Thread(target=_pump, args=(up, self.connection), daemon=True)
        t.start()
        _pump(self.connection, up)
        t.join(timeout=5)
        up.close()

    def _plain(self):
        """Plain-http request sent through the proxy (absolute URI): forward untouched."""
        u = urllib.parse.urlsplit(self.path)
        if not u.hostname:
            return self.send_error(400, "proxy requests need an absolute URI")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)
        try:
            conn.request(self.command, (u.path or "/") + ("?" + u.query if u.query else ""), body,
                         {k: v for k, v in self.headers.items() if k.lower() not in HOP})
            r = conn.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, http.client.HTTPException) as e:
            self.send_error(502, str(e))
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _plain


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else PORT
    if cmd == "serve":
        srv = ExclusiveServer(("127.0.0.1", port), Handler)     # loopback only
        log("claude-gateway on http://127.0.0.1:%d -> %s (free fallback %s, model %s)" % (port, UPSTREAM, FREE_BASE, FREE_MODEL))
        if "--no-proxy" not in argv:
            psrv = ExclusiveServer(("127.0.0.1", PROXY_PORT), ProxyHandler)    # loopback only
            threading.Thread(target=psrv.serve_forever, daemon=True).start()
            log("forward proxy on 127.0.0.1:%d (intercepts only %s; certificate %s)" % (
                PROXY_PORT, ",".join(sorted(INTERCEPT_HOSTS)), "present" if tls_context() else "NOT installed"))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    elif cmd == "status":
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/__status" % port, timeout=5) as r:
                s = json.loads(r.read())
        except Exception as e:
            print("not running (%s)" % e)
            return 1
        print("serving: %s" % s["serving"] + ("  (%s, back to Claude in %ds)" % (s["reason"], s["limited_for_s"]) if s["limited_for_s"] else ""))
        print("requests moved to free-chain: %d | kept on Claude (chat not on free list, or work): %d" % (s["to_free"], s["blocked"]))
        print("chats on the free list: %d session id(s) in %s" % (len(free_sessions()), CONFIG))
        print("'free' requests answered by Claude instead (fallback): %d%s" % (
            s.get("claude_fallbacks", 0),
            ("  | free chain skipped for another %ds" % s["free_down_for_s"]) if s.get("free_down_for_s") else ""))
        print("work folders (no automatic failover; Opus 4.6 chosen by hand still goes free): " + ", ".join(s["protected"]))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
