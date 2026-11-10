#!/usr/bin/env python3
"""migrate-sessions: point existing desktop-app Code chats at an environment (e.g. 'Free Chat').

Each desktop chat record has an envScopeId (whose environment variables the chat gets when the app next
starts its Claude Code process) and a model. This script changes only those two fields, only for the chats
you name, and records the old values so --undo restores exactly those fields (never whole files).

SAFETY
  * Work-group chats (folders in ~/.model-failover/claude-gateway.json) are never touched.
  * Refuses to run while the Claude desktop app is open (it would overwrite the edit).
  * Unsupported by the app: it edits the app's own chat files. Verify one chat afterwards.

  python migrate-sessions.py                                   # list chats, their environment and model
  python migrate-sessions.py --to "Free Chat" --only "skills,youtube" [--model claude-opus-4-6] [--apply]
  python migrate-sessions.py --fix-dangling [--apply]          # chats pointing at a deleted environment -> default
  python migrate-sessions.py --undo                            # restore the fields changed by the last --apply

FREE LIST (opt-in: only chats you name use free models; every other chat is normal Claude)
  python migrate-sessions.py --free-set "youtube manager" [--apply]   # exactly these chats are free
  python migrate-sessions.py --free-add "spotify" [--apply]           # add to the free list
  python migrate-sessions.py --free-remove "youtube" [--apply]        # back to normal Claude
  Each one moves the named chats to the 'Free Chat' environment on the free model, moves every other chat off
  it (restoring the model it had before it was moved, else Opus 5.5), and saves the gateway's free list.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data")
CFG = os.path.join(HOME, ".model-failover", "claude-gateway.json")
ENV_RE = re.compile(r'"envScopeId"\s*:\s*"([^"]*)"')
MODEL_RE = re.compile(r'("model"\s*:\s*)"([^"]*)"')
FREE_MODEL = "claude-opus-4-6"        # mapped to the free chain by modelOverrides in ~/.claude/settings.json


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def app_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    return "Claude.exe" in out


def protected():
    try:
        with open(CFG, encoding="utf-8") as f:
            return [s.lower() for s in json.load(f)["never_free_dirs"]]
    except (OSError, ValueError, KeyError):
        return []


def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CLAUDE_DEFAULT = "claude-opus-5-5"


def models_before_free(backups):
    """file -> the model a chat had before an earlier migration put it on the free model (oldest record wins)."""
    out = {}
    for run in sorted(glob.glob(os.path.join(backups, "*.json")), reverse=True):
        try:
            with open(run, encoding="utf-8") as f:
                for path, old in json.load(f).items():
                    m = old.get("model")
                    if m and m != FREE_MODEL:
                        out[path] = m
        except (OSError, ValueError):
            pass
    return out


def cli_ids(j):
    return {str(j[k]).lower() for k in ("cliSessionId", "lastAssistantCliSessionId") if j.get(k)}


def presets(base):
    out = {}
    for p in glob.glob(os.path.join(base, "**", "ccd-session-presets.json"), recursive=True):
        with open(p, encoding="utf-8") as f:
            for acct in json.load(f).get("byAccount", {}).values():
                for pr in acct.get("presets", []):
                    out[pr["id"]] = pr["name"]
    return out


def chats(base):
    rows = []
    for f in glob.glob(os.path.join(base, "**", "claude-code-sessions", "**", "local_*.json"), recursive=True):
        raw = open(f, encoding="utf-8").read()
        try:
            j = json.loads(raw)
        except ValueError:
            continue
        where = " ".join(str(j.get(k, "")) for k in ("cwd", "originCwd")).lower()
        in_work_folder = any(p in where for p in protected())
        ids = cli_ids(j)
        rows.append({"file": f, "raw": raw, "title": j.get("title") or "(untitled)", "scope": j.get("envScopeId"),
                     "model": j.get("model"), "ids": ids, "in_work_folder": in_work_folder,
                     "work": in_work_folder,
                     "mtime": os.path.getmtime(f)})
    return sorted(rows, key=lambda r: -r["mtime"])


def set_env(raw, value):
    """Return raw with envScopeId set to value (None removes the key)."""
    if value is None:
        raw = re.sub(r',\s*"envScopeId"\s*:\s*"[^"]*"', "", raw, count=1)
        return ENV_RE.sub("", raw, count=1) if ENV_RE.search(raw) else raw
    if ENV_RE.search(raw):
        return ENV_RE.sub('"envScopeId":"%s"' % value, raw, count=1)
    body = raw.rstrip()
    return body[:-1] + ',"envScopeId":"%s"}' % value + raw[len(body):]


def write(path, raw):
    json.loads(raw)                                  # never write something that is not valid JSON
    with open(path, "w", encoding="utf-8", newline="") as out:
        out.write(raw)


def free_list(base, backups, pre, op, apply_, force):
    keys = [k.strip().lower() for k in (arg(op) or "").split(",") if k.strip()]
    if not keys:
        print('name chats by part of their title, e.g. %s "youtube manager"' % op); return 1
    free_env = next((i for i, n in pre.items() if n.lower() == "free chat"), None)
    if not free_env:
        print("no environment named 'Free Chat' in the app. Create it first."); return 1
    rows = chats(base)
    named = [r for r in rows if any(k in r["title"].lower() for k in keys)]
    for r in named:
        if r["work"]:
            print("   skipped (work chat, never free): %s" % r["title"])
    named = [r for r in named if not r["work"]]
    if not named and op != "--free-remove":
        print("no non-work chat title contains: %s" % ", ".join(keys)); return 1
    on_free = lambda r: r["scope"] == free_env or r["model"] == FREE_MODEL
    current = {r["file"] for r in rows if not r["work"] and on_free(r)}
    files = {r["file"] for r in named}
    want = files if op == "--free-set" else (current | files if op == "--free-add" else current - files)

    before = models_before_free(backups)
    todo = []
    for r in rows:
        if r["work"]:
            continue
        if r["file"] in want and not (r["scope"] == free_env and r["model"] == FREE_MODEL):
            todo.append((r, free_env, FREE_MODEL))
        elif r["file"] not in want and on_free(r):
            m = r["model"] if r["model"] != FREE_MODEL else before.get(r["file"], CLAUDE_DEFAULT)
            todo.append((r, None, m))
    keep = [r for r in rows if r["file"] in want]
    print("free list after this: %s" % (", ".join(sorted({r["title"] for r in keep})) or "(empty)"))
    for r, target, m in todo:
        print("   - %-40s -> %s, %s" % (r["title"][:40], "Free Chat" if target else "default (Claude)", m))
    if not todo:
        print("   chats already match.")
    if not apply_:
        print("\n(dry run) add --apply to make the change."); return 0
    if todo and app_running() and not force:
        print("\nQuit the Claude desktop app first (system tray > Quit), then run again."); return 2
    if todo:
        os.makedirs(backups, exist_ok=True)
        changes = {}
        for r, target, m in todo:
            changes[r["file"]] = {"scope": r["scope"], "model": r["model"]}
            raw = MODEL_RE.sub(lambda x: x.group(1) + '"%s"' % m, set_env(r["raw"], target), count=1)
            write(r["file"], raw)
        bk = os.path.join(backups, time.strftime("%Y%m%d-%H%M%S") + ".json")
        with open(bk, "w", encoding="utf-8") as out:
            json.dump(changes, out, indent=2)
        print("\nchanged %d chats. undo: python migrate-sessions.py --undo" % len(todo))
    cfg = load_cfg()
    cfg.pop("free_ok_sessions", None)
    cfg["free_sessions"] = sorted(set().union(*[r["ids"] for r in keep]) if keep else set())
    with open(CFG, "w", encoding="utf-8") as out:
        json.dump(cfg, out, indent=2)
    print("gateway free list saved (%d session id(s)); takes effect on the next message." % len(cfg["free_sessions"]))
    return 0


def main():
    global CFG
    CFG = arg("--cfg", CFG)
    base = arg("--data", DATA)
    backups = arg("--backups", os.path.join(HOME, ".model-failover", "session-backups"))
    force, apply_ = "--force" in sys.argv, "--apply" in sys.argv
    pre = presets(base)

    op = next((o for o in ("--free-set", "--free-add", "--free-remove") if o in sys.argv), None)
    if op:
        return free_list(base, backups, pre, op, apply_, force)

    if "--undo" in sys.argv:
        runs = sorted(glob.glob(os.path.join(backups, "*.json")))
        if not runs:
            print("no backup found"); return 1
        if app_running() and not force:
            print("Quit the Claude desktop app first (system tray > Quit), then run again."); return 2
        with open(runs[-1], encoding="utf-8") as f:
            changes = json.load(f)
        for path, old in changes.items():
            raw = open(path, encoding="utf-8").read()
            raw = set_env(raw, old["scope"])
            if old.get("model") is not None:
                raw = MODEL_RE.sub(lambda m: m.group(1) + '"%s"' % old["model"], raw, count=1)
            write(path, raw)
        print("restored the environment/model fields on %d chats (nothing else touched), from %s" % (len(changes), runs[-1]))
        return 0

    rows = chats(base)
    todo = []
    if "--fix-dangling" in sys.argv:
        for r in rows:
            if not r["work"] and r["scope"] and r["scope"] != "builtin_local" and r["scope"] not in pre:
                todo.append((r, None, None))
        mode = "chats pointing at a deleted environment -> back to default"
    elif "--to" in sys.argv:
        name = arg("--to")
        if name.lower() == "default":
            target = None                                  # back to the plain default environment
        else:
            target = next((i for i, n in pre.items() if n.lower() == name.lower()), None)
            if not target:
                print("no environment named %r. Existing: %s, default" % (name, ", ".join(pre.values()))); return 1
        keys = [k.strip().lower() for k in (arg("--only", "") or "").split(",") if k.strip()]
        everything = "--all-non-work" in sys.argv
        if not keys and not everything:
            print("give --only \"part of chat title, another\" or --all-non-work, so nothing is migrated by accident."); return 1
        for r in rows:
            if r["work"] or r["scope"] == target:
                continue
            if everything or any(k in r["title"].lower() for k in keys):
                todo.append((r, target, arg("--model")))
        mode = "chats to point at %r" % name
    else:
        print("%-3s %-40s %-18s %s" % ("", "chat", "environment", "model"))
        for r in rows[:40]:
            env = "WORK (never)" if r["work"] else (pre.get(r["scope"]) or ("DELETED env" if r["scope"] and r["scope"] != "builtin_local" else (r["scope"] or "-")))
            print("%-3s %-40s %-18s %s" % ("", r["title"][:40], env[:18], r["model"] or ""))
        return 0

    print("%s: %d" % (mode, len(todo)))
    for r, _, m in todo:
        print("   - %s%s" % (r["title"][:70], ("   (model -> %s)" % m) if m else ""))
    if not apply_:
        print("\n(dry run) add --apply to make the change."); return 0
    if not todo:
        return 0
    if app_running() and not force:
        print("\nQuit the Claude desktop app first (system tray > Quit), then run again."); return 2
    os.makedirs(backups, exist_ok=True)
    changes = {}
    for r, target, model in todo:
        changes[r["file"]] = {"scope": r["scope"], "model": r["model"] if model else None}
        raw = set_env(r["raw"], target)
        if model:
            raw = MODEL_RE.sub(lambda m: m.group(1) + '"%s"' % model, raw, count=1)
        write(r["file"], raw)
    bk = os.path.join(backups, time.strftime("%Y%m%d-%H%M%S") + ".json")
    with open(bk, "w", encoding="utf-8") as out:
        json.dump(changes, out, indent=1)
    print("\nchanged %d chats. undo (restores just those fields): python migrate-sessions.py --undo\nrecord: %s" % (len(todo), bk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
