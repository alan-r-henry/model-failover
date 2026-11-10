#!/usr/bin/env python3
"""free-resume: pick one of your recent Claude Code chats and carry on with it on free models.

Use it after the Claude 5-hour/weekly limit stops a chat (desktop app or terminal). It lists
your most recent chats, you type a number, and it reopens that exact chat via claude-free.ps1.
Work-group projects are listed as protected and cannot be picked.
"""
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")
CFG = os.path.join(os.path.expanduser("~"), ".model-failover", "claude-gateway.json")
HERE = os.path.dirname(os.path.abspath(__file__))
SHOW = 10


def protected_names():
    try:
        with open(CFG, encoding="utf-8") as f:
            return [s.lower() for s in json.load(f)["never_free_dirs"]]
    except (OSError, ValueError, KeyError):
        return []


def read_tail(path, nbytes=400_000):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - nbytes))
        return fh.read().decode("utf-8", "replace").splitlines()[1:]


def describe(path):
    cwd = title = last = None
    for line in read_tail(path):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        cwd = o.get("cwd") or cwd
        if o.get("type") == "custom-title":
            title = o.get("customTitle") or title
        elif o.get("type") == "last-prompt":
            last = o.get("lastPrompt") or last
    return cwd, title, last


def ago(ts):
    s = int(time.time() - ts)
    return "%dm" % (s // 60) if s < 3600 else ("%dh" % (s // 3600) if s < 86400 else "%dd" % (s // 86400))


def main():
    files = [f for f in glob.glob(os.path.join(ROOT, "*", "*.jsonl"))]
    files.sort(key=os.path.getmtime, reverse=True)
    prot = protected_names()
    rows = []
    for f in files[:40]:
        cwd, title, last = describe(f)
        if not cwd or "\\appdata\\local\\temp\\" in cwd.lower():
            continue
        rows.append({"id": os.path.splitext(os.path.basename(f))[0], "cwd": cwd, "age": ago(os.path.getmtime(f)),
                     "name": (title or last or "(untitled)").replace("\n", " ")[:46],
                     "protected": any(p in cwd.lower() for p in prot)})
        if len(rows) >= SHOW:
            break
    if not rows:
        print("No recent chats found.")
        return 1
    print("\nContinue a chat on FREE models (NVIDIA / OpenRouter free). Work projects are protected.\n")
    n = 0
    for r in rows:
        if r["protected"]:
            print("   -  %-4s %-46s %s   [protected: not offered]" % (r["age"], r["name"], os.path.basename(r["cwd"])))
        else:
            n += 1
            r["n"] = n
            print("  %2d  %-4s %-46s %s" % (n, r["age"], r["name"], os.path.basename(r["cwd"])))
    pick = input("\nNumber to resume (Enter to cancel): ").strip()
    chosen = next((r for r in rows if str(r.get("n")) == pick), None)
    if not chosen:
        return 0
    print("\nResuming '%s' in %s on free models...\n" % (chosen["name"], chosen["cwd"]))
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            os.path.join(HERE, "claude-free.ps1"), "-Folder", chosen["cwd"], "-Resume", chosen["id"]])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
