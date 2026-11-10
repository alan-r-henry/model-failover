#!/usr/bin/env python3
"""model-failover: a local OpenAI-compatible gateway that walks an ordered list of
providers ("tiers") and moves to the next one when the current one is out of
credits / rate limited / down. Stdlib only.

  python failover.py serve [--port 20130]   run the gateway (127.0.0.1 only)
  python failover.py status                 show which tier is active and why others are cooling
  python failover.py test                   send one tiny request through the gateway

Point any OpenAI-compatible client at http://127.0.0.1:20130/v1 with model "auto".
Config: ~/.model-failover/tiers.json (created on first run, edit freely).
API keys are never stored in the config: each tier names an environment variable.
"""
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket

class ExclusiveServer(ThreadingHTTPServer):
    """Refuses a port another process already holds. Python's default SO_REUSEADDR lets a second copy
    share the port on Windows, and requests then go to whichever copy (possibly old code) Windows picks."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


HOME = os.path.join(os.path.expanduser("~"), ".model-failover")
CONFIG = os.environ.get("FAILOVER_CONFIG", os.path.join(HOME, "tiers.json"))
LOG = os.path.join(HOME, "failover.log")
DEFAULT_PORT = 20130

DEFAULT_TIERS = {
    "tiers": [
        {"name": "omniroute", "base_url": "http://localhost:20128/v1",
         "key_env": "OMNIROUTE_API_KEY",
         "model": "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b", "timeout": 60},
        {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1",
         "key_env": "OPENROUTER_API_KEY", "model": "openrouter/free", "timeout": 60},
        {"name": "opencode", "base_url": "https://opencode.ai/zen/v1",
         "key_env": None, "key_literal": "public", "model": "big-pickle", "timeout": 60},
    ]
}

# Cooldown (seconds) per failure kind. Rate-limit replies override this with the
# server's own reset time when they give one.
COOL_RATE_LIMIT_DEFAULT = 3600
COOL_PAYMENT = 6 * 3600       # 402: credits exhausted
COOL_AUTH = 1800              # 401/403: bad or missing key
COOL_NOT_FOUND = 600          # 404: model/endpoint gone
COOL_TRANSIENT = 60           # 5xx, timeout, connection refused
COOL_MAX = 24 * 3600

_lock = threading.Lock()
_cooling = {}   # tier name -> (until_epoch, reason)
_stats = {}     # tier name -> {"ok": n, "fail": n}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def load_tiers():
    if not os.path.exists(CONFIG):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_TIERS, f, indent=2)
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)["tiers"]


def _user_env(name):
    """Windows: value saved in the user's environment (HKCU), even if this process
    started before it was set. Lets the gateway pick up keys added later."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return ""


def tier_key(t):
    if t.get("key_env"):
        return os.environ.get(t["key_env"]) or _user_env(t["key_env"])
    return t.get("key_literal", "")


def cooldown_seconds(status, headers, body):
    """How long to rest a tier after an HTTP error. status=None means network error."""
    if status is None or status >= 500 or status == 408:
        return COOL_TRANSIENT, "unreachable/5xx"
    if status == 402:
        return COOL_PAYMENT, "credits exhausted (402)"
    if status in (401, 403):
        return COOL_AUTH, "auth rejected (%d)" % status
    if status == 404:
        return COOL_NOT_FOUND, "not found (404)"
    if status == 429:
        secs = None
        ra = headers.get("Retry-After")
        if ra and ra.strip().isdigit():
            secs = int(ra)
        reset = headers.get("X-RateLimit-Reset")  # OpenRouter: epoch milliseconds
        if secs is None and reset and reset.strip().isdigit():
            secs = max(0, int(reset) / 1000.0 - time.time())
        if secs is None:
            m = re.search(r'"reset_seconds"\s*:\s*(\d+)', body or "")
            if m:
                secs = int(m.group(1))
        return min(secs if secs is not None else COOL_RATE_LIMIT_DEFAULT, COOL_MAX), "rate limited/quota (429)"
    return 0, "client error (%s)" % status   # 400/422 etc: try next tier, don't punish this one


def mark(name, status, headers, body):
    secs, reason = cooldown_seconds(status, headers, body)
    with _lock:
        _stats.setdefault(name, {"ok": 0, "fail": 0})["fail"] += 1
        if secs > 0:
            _cooling[name] = (time.time() + secs, reason)
    detail = re.sub(r"\s+", " ", body or "")[:160]
    log("tier '%s' failed: %s%s | %s" % (name, reason, (", resting %ds" % secs) if secs else "", detail))


def available(t):
    with _lock:
        c = _cooling.get(t["name"])
        if c and c[0] > time.time():
            return False
        _cooling.pop(t["name"], None)
    return True


def forward(tier, payload):
    """Open the upstream request. Returns (resp, None) or (None, (status, headers, body))."""
    p = dict(payload)
    p["model"] = tier["model"]
    req = urllib.request.Request(
        tier["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(p).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + tier_key(tier),
                 "User-Agent": "model-failover/1.0"},
        method="POST")
    try:
        return urllib.request.urlopen(req, timeout=tier.get("timeout", 60)), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return None, (e.code, e.headers, body)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return None, (None, {}, str(e))


class ClientGone(Exception):
    """The calling client closed its connection; not a provider failure."""


class Handler(BaseHTTPRequestHandler):
    server_version = "model-failover/1.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", ""):
            return self._json(200, {"ok": True})
        if path == "/v1/models":
            return self._json(200, {"object": "list", "data": [
                {"id": "auto", "object": "model", "owned_by": "model-failover"}]})
        if path == "/status":
            now = time.time()
            rows = []
            for t in load_tiers():
                c = _cooling.get(t["name"])
                resting = c and c[0] > now
                rows.append({
                    "tier": t["name"], "model": t["model"],
                    "key_present": bool(tier_key(t)),
                    "state": ("resting" if resting else ("no key" if not tier_key(t) else "ready")),
                    "reason": c[1] if resting else None,
                    "back_in_s": int(c[0] - now) if resting else 0,
                    **_stats.get(t["name"], {"ok": 0, "fail": 0})})
            active = next((r["tier"] for r in rows if r["state"] == "ready"), None)
            return self._json(200, {"active": active, "tiers": rows})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": "only /v1/chat/completions is proxied"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        last = None
        tiers = [t for t in load_tiers() if tier_key(t)]
        order = [t for t in tiers if available(t)]
        if not order:
            # Everything is resting. Rather than refuse, probe the tier that recovers
            # soonest: a blip must not lock the whole chain out for a minute.
            with _lock:
                order = sorted(tiers, key=lambda t: _cooling.get(t["name"], (0,))[0])[:1]
        for tier in order:
            resp, err = forward(tier, payload)
            if err:
                last = err
                if os.environ.get("FAILOVER_DEBUG"):
                    with open(os.path.join(HOME, "last-failed-%s.json" % tier["name"]), "w", encoding="utf-8") as f:
                        json.dump({"tier": tier["name"], "payload": payload}, f)
                mark(tier["name"], *err)
                continue
            try:
                self._relay(tier, resp, bool(payload.get("stream")))
                with _lock:
                    _stats.setdefault(tier["name"], {"ok": 0, "fail": 0})["ok"] += 1
                return
            except ClientGone:
                # The caller hung up (e.g. opencode abandons its title request). That is
                # not the provider's fault: do not penalise the tier, just stop.
                return
            except (TimeoutError, OSError, http.client.HTTPException) as e:
                # Failed before we sent anything to the client -> safe to try next tier.
                last = (None, {}, str(e))
                if os.environ.get("FAILOVER_DEBUG"):
                    with open(os.path.join(HOME, "last-failed-%s.json" % tier["name"]), "w", encoding="utf-8") as f:
                        json.dump({"tier": tier["name"], "payload": payload}, f)
                mark(tier["name"], *last)
                if getattr(self, "_started", False):
                    return          # stream already begun; cannot restart it
        msg = "all tiers unavailable"
        if last:
            msg += " (last error: %s %s)" % (last[0], (last[2] or "")[:200])
        self._json(503, {"error": {"message": msg, "type": "all_tiers_exhausted"}})

    def _to_client(self, fn, *a):
        """Run a write to the client; any socket error means the CLIENT left."""
        try:
            return fn(*a)
        except OSError as e:
            raise ClientGone(str(e))

    def _relay(self, tier, resp, stream):
        self._started = False
        ctype = resp.headers.get("Content-Type", "application/json")
        read = resp.read1 if hasattr(resp, "read1") else resp.read
        try:
            if not stream:
                body = resp.read()            # upstream errors raise here -> next tier
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Failover-Tier", tier["name"])
                self._to_client(self.end_headers)
                self._to_client(self.wfile.write, body)
                return
            first = read(8192)                # upstream errors raise here -> next tier
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Failover-Tier", tier["name"])
            self._to_client(self.end_headers)
            self._started = True
            if first:
                self._to_client(self.wfile.write, first)
                self._to_client(self.wfile.flush)
            while True:
                chunk = read(8192)            # upstream error mid-stream -> OSError
                if not chunk:
                    break
                self._to_client(self.wfile.write, chunk)
                self._to_client(self.wfile.flush)
        finally:
            resp.close()


def serve(port):
    load_tiers()
    srv = ExclusiveServer(("127.0.0.1", port), Handler)   # loopback only, on purpose
    log("model-failover listening on http://127.0.0.1:%d/v1 (config %s)" % (port, CONFIG))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def _get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
        return json.loads(r.read())


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    port = DEFAULT_PORT
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if cmd == "serve":
        serve(port)
    elif cmd == "status":
        try:
            s = _get(port, "/status")
        except Exception as e:
            print("gateway not running on port %d (%s)" % (port, e))
            return 1
        print("active tier:", s["active"] or "NONE - every tier is resting")
        for r in s["tiers"]:
            extra = (" - %s, back in %ds" % (r["reason"], r["back_in_s"])) if r["state"] == "resting" else ""
            print("  %-11s %-8s ok=%d fail=%d  %s%s" % (r["tier"], r["state"], r["ok"], r["fail"], r["model"], extra))
    elif cmd == "test":
        body = json.dumps({"model": "auto", "max_tokens": 200, "messages": [
            {"role": "user", "content": "Reply with exactly the word: pong"}]}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
                print("served by tier:", r.headers.get("X-Failover-Tier"))
                print("reply:", (d["choices"][0]["message"].get("content") or "").strip())
        except urllib.error.HTTPError as e:
            print("FAILED", e.code, e.read().decode()[:300])
            return 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
