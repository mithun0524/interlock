"""`interlock init` — the entire onboarding.

No .gitignore edit is needed: state lives inside .git, which is never
tracked.
"""

import json
import os
import pathlib

HOOK_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")
MATCHER = "Edit|Write|NotebookEdit|Bash"
_MARK = "interlock hook"


def _command(event):
    return f"interlock hook --event {event}"


def hook_config():
    hooks = {}
    for event in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": _command(event)}]}
        if event in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = MATCHER
        hooks[event] = [entry]
    return {"hooks": hooks}


def merge(existing):
    out = dict(existing)
    hooks = dict(out.get("hooks", {}))
    for event, entries in hook_config()["hooks"].items():
        current = [
            e for e in hooks.get(event, [])
            if not any(_MARK in h.get("command", "")
                       for h in e.get("hooks", []))
        ]
        hooks[event] = current + entries
    out["hooks"] = hooks
    return out


def install(root, local=False):
    name = "settings.local.json" if local else "settings.json"
    d = pathlib.Path(root) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except json.JSONDecodeError:
            existing = {}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merge(existing), indent=2))
    os.replace(tmp, p)
    return str(p)
