"""One binary, two faces: `agentmesh hook` for hooks, the rest for agents
and humans."""

import argparse
import json
import os
import sys

from . import detect, hooks, install, negotiate, state


def resolve_agent_id(explicit=None):
    if explicit:
        return explicit
    # Explicit beats ambient: AGENTMESH_ID is something a human or script
    # set on purpose, while a session id is merely inherited from whatever
    # process happens to be the parent.
    env = os.environ.get("AGENTMESH_ID")
    if env:
        return env
    # CLAUDE_CODE_SESSION_ID is the name Claude Code actually exports;
    # CLAUDE_SESSION_ID is kept only as a fallback for other harnesses.
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        sid = os.environ.get(var)
        if sid:
            return sid[:6]
    here = state.worktree_root()
    mine = [aid for aid, row in state.get_all().items()
            if row.get("worktree") == here]
    if len(mine) == 1:
        return mine[0]
    raise SystemExit(
        "agentmesh: cannot tell which agent you are. Pass --id, or set "
        "AGENTMESH_ID."
    )


def _print_conflicts(conflicts):
    if not conflicts:
        print("agentmesh: declared, no conflicts.")
        return
    blocking = [c for c in conflicts if c.get("status") != "resolved"]
    print(f"agentmesh: declared, {len(conflicts)} conflict(s) "
          f"({len(blocking)} blocking):")
    for c in conflicts:
        flag = " (advisory)" if c.get("status") == "resolved" else ""
        print(f"  [{c['kind']}]{flag} {c['ref'] or '(files)'} with "
              f"{c.get('with') or '(repo-wide)'}: {c['detail']}")


def cmd_declare(args):
    agent_id = resolve_agent_id(args.id)
    deltas = ([{"kind": "remove", "symbol": s} for s in args.removes]
              + [{"kind": "sig", "symbol": s} for s in args.sig]
              + [{"kind": "add", "symbol": s} for s in args.adds])
    try:
        detect.validate_symbols(deltas, args.deps)
    except detect.SymbolTooVague as e:
        print(f"agentmesh: {e}", file=sys.stderr)
        return 1

    root = state.worktree_root()
    fields = {
        "goal": args.goal, "globs": args.globs, "deltas": deltas,
        "deps": args.deps, "worktree": root,
    }
    if args.progress is not None:
        fields["progress"] = args.progress

    # Detect against the PROSPECTIVE row, then write fields and conflicts in
    # one upsert. Writing fields first and conflicts second leaves a window
    # where the declaration is live and advertises no conflicts, and another
    # agent's gate reading in that window sails straight through it.
    agents = state.get_all()
    prior_row = agents.get(agent_id, {})
    me = dict(prior_row)
    me.update(fields)
    agents = dict(agents)
    agents[agent_id] = me

    conflicts = detect.detect_all(me, agents, root, my_id=agent_id)
    # preserve status/round on conflicts already under negotiation
    prior = {(c["kind"], c["ref"], c.get("with")): c
             for c in prior_row.get("conflicts", [])}
    for c in conflicts:
        was = prior.get((c["kind"], c["ref"], c.get("with")))
        if was:
            c.update({k: was[k] for k in
                      ("status", "round", "waiting_since_inv", "nudged",
                       "auto_objected", "escalated_at_inv")
                      if k in was})

    # Mark objections before the write so the conflict statuses land in the
    # same single upsert; the messages themselves go out afterwards, since
    # inbox writes cannot tear the declaration.
    pending = negotiate.plan_objections(agent_id, conflicts, agents)

    state.upsert(agent_id, conflicts=conflicts, **fields)
    negotiate.send_planned(pending)

    _print_conflicts(conflicts)
    if pending:
        print(f"agentmesh: {len(pending)} objection(s) raised on your behalf "
              f"by the agents that depend on this. Answer with "
              f"`agentmesh reply --kind offer`.")
    return 0


def cmd_status(args):
    negotiate.check_timers()
    agents = state.get_all()
    if args.json:
        print(json.dumps({"agents": agents,
                          "escalations": negotiate.escalations()}, indent=2))
        return 0
    if not agents:
        print("agentmesh: no active agents.")
        return 0
    print(f"agentmesh: {len(agents)} active agent(s)")
    for aid, row in sorted(agents.items()):
        print(f"\n  {aid}  {row.get('goal', '')}")
        print(f"    worktree: {row.get('worktree', '')}")
        if row.get("globs"):
            print(f"    globs:    {', '.join(row['globs'])}")
        if row.get("progress"):
            print(f"    produced: {row['progress']}")
        for c in row.get("conflicts", []):
            print(f"    [{c['status']}] {c['kind']} {c['ref']} "
                  f"with {c.get('with') or '(repo-wide)'}")
    esc = negotiate.escalations()
    if esc:
        print("\nDECISION NEEDED")
        for c in esc:
            print(f"  {c['ref']}: {c['detail']}")
            print(f"    agent {c['agent']} vs {c.get('with') or '(repo)'}")
            print(f"    agentmesh resolve --ref {c['ref']} "
                  f"--decision \"<verdict>\"")
    return 0


def cmd_reply(args):
    agent_id = resolve_agent_id(args.id)
    if args.to not in state.get_all():
        print(f"agentmesh: no active agent {args.to!r}. "
              f"Run `agentmesh status` to see who is here.", file=sys.stderr)
        return 1
    msg = negotiate.send(agent_id, args.to, args.kind, args.ref, args.msg)
    try:
        negotiate.apply(msg)
    except negotiate.ForgedAccept as e:
        print(f"agentmesh: {e}", file=sys.stderr)
        return 1
    print(f"agentmesh: {args.kind} sent to {args.to} re {args.ref}")
    return 0


_AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def _looks_like_an_agent():
    """Guardrail, not security.

    The trust model has no boundary between agents -- anything here is
    bypassable by an agent that means to. But observed live, an agent reached
    for `agentmesh resolve` to unblock itself the moment its counterparty went
    away, which skips the negotiation entirely. That is a larger bypass than
    the forged accept that IS guarded, so the path worth closing is the one an
    agent actually takes.
    """
    if os.environ.get("AGENTMESH_ALLOW_AGENT_RESOLVE"):
        return False
    return any(os.environ.get(v) for v in _AGENT_ENV)


def cmd_resolve(args):
    if _looks_like_an_agent():
        print(
            "agentmesh: resolve records a HUMAN verdict and cannot be run from "
            "inside an agent session.\n"
            f"  Negotiate instead: agentmesh reply --kind offer --ref "
            f"{args.ref} --msg \"<a shim, a migration, or another approach>\"\n"
            "  If the other agent is gone, it is released automatically once "
            "its heartbeat goes stale.\n"
            "  Automation that legitimately needs this: set "
            "AGENTMESH_ALLOW_AGENT_RESOLVE=1.",
            file=sys.stderr,
        )
        return 1
    n = negotiate.resolve(args.ref, args.decision)
    print(f"agentmesh: verdict recorded on {args.ref}, "
          f"{n} conflict(s) resolved.")
    return 0


def cmd_release(args):
    agent_id = resolve_agent_id(args.id)
    state.upsert(agent_id, state="done")
    print(f"agentmesh: {agent_id} released its claims.")
    return 0


def cmd_hook(args):
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0                       # never break the agent on bad input
    code, out, err = hooks.run(args.event, payload)
    if err:
        print(err, file=sys.stderr)
    if out:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": args.event,
            "additionalContext": out,
        }}))
    return code


def cmd_init(args):
    path = install.install(state.worktree_root(), local=args.local)
    scope = "this developer only" if args.local else "everyone who clones"
    print(f"agentmesh: hooks installed in {path} ({scope}).")
    print("Agents in this repo will now be asked to declare before editing.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agentmesh")
    p.add_argument("--id", help="agent id (default: session id prefix)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("declare", help="register or update your intent")
    d.add_argument("--goal", required=True)
    d.add_argument("--globs", nargs="*", default=[])
    d.add_argument("--removes", nargs="*", default=[])
    d.add_argument("--sig", nargs="*", default=[])
    d.add_argument("--adds", nargs="*", default=[])
    d.add_argument("--deps", nargs="*", default=[])
    d.add_argument("--progress")
    d.set_defaults(func=cmd_declare)

    s = sub.add_parser("status", help="roster, conflicts, pending decisions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("reply", help="send a negotiation message")
    r.add_argument("--to", required=True)
    r.add_argument("--kind", required=True, choices=negotiate.KINDS)
    r.add_argument("--ref", required=True)
    r.add_argument("--msg", required=True)
    r.set_defaults(func=cmd_reply)

    v = sub.add_parser("resolve", help="record a human verdict")
    v.add_argument("--ref", required=True)
    v.add_argument("--decision", required=True)
    v.set_defaults(func=cmd_resolve)

    rel = sub.add_parser("release", help="drop your claims")
    rel.set_defaults(func=cmd_release)

    h = sub.add_parser("hook", help="internal; reads hook JSON on stdin")
    h.add_argument("--event", required=True)
    h.set_defaults(func=cmd_hook)

    i = sub.add_parser("init", help="install the hooks into this repo")
    i.add_argument("--local", action="store_true",
                   help="write settings.local.json instead of settings.json")
    i.set_defaults(func=cmd_init)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not hasattr(args, "id"):
        args.id = None
    return args.func(args)
