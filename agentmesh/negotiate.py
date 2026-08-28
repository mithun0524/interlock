"""Messages between agents.

Verb roles are asymmetric on purpose: the blocked agent carries the burden
of proposing. Without that, both sides wait for the other to move.

  objection  sent by the dependent agent  "I rely on ref, do not remove it"
  offer      sent by the blocked agent     a concrete way forward
  accept     reply to an offer             agreed; terms recorded
  refuse     reply to an offer             rejected, counter-offer in msg
"""

import json
import time

from . import state

KINDS = ("objection", "offer", "accept", "refuse")


class ForgedAccept(Exception):
    """An accept whose sender is the agent it would unblock."""


def _inbox(agent_id):
    return state.state_dir() / "inbox" / f"{agent_id}.jsonl"


def _current_round(agents, frm, to, ref):
    for aid in (frm, to):
        for c in agents.get(aid, {}).get("conflicts", []):
            if c.get("ref") == ref:
                return c.get("round", 0)
    return 0


def send(frm, to, kind, ref, msg):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    agents = state.get_all()
    recipient = agents.get(to, {})
    record = {
        "from": frm, "to": to, "kind": kind, "ref": ref, "msg": msg,
        "round": _current_round(agents, frm, to, ref),
        "sent_at_inv": recipient.get("inv", 0),
        "ts": time.time(),
    }
    with state.locked():
        p = _inbox(to)
        with open(p, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    if kind == "offer":
        mark_waiting(frm, ref, record["sent_at_inv"])
    return record


def drain(agent_id):
    with state.locked():
        p = _inbox(agent_id)
        if not p.exists():
            return []
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        p.write_text("")                 # truncated the moment it is drained
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def format_for_agent(msgs):
    if not msgs:
        return ""
    lines = ["agentmesh: messages from other agents:"]
    for m in msgs:
        lines.append(
            f"  [{m['kind']}] from {m['from']} re {m['ref'] or '(files)'}: "
            f"{m['msg']}"
        )
        if m["kind"] == "objection":
            lines.append(
                f"      you are blocked, so you propose: agentmesh reply "
                f"--to {m['from']} --kind offer --ref {m['ref']} "
                f"--msg \"<shim / migration / other approach>\""
            )
        if m["kind"] == "offer":
            lines.append(
                f"      answer it: agentmesh reply --to {m['from']} "
                f"--kind accept|refuse --ref {m['ref']} --msg \"<why>\""
            )
    return "\n".join(lines)


def apply(msg):
    """Mutates the recipient's declaration according to the verb."""
    kind, ref, frm, to = msg["kind"], msg["ref"], msg["from"], msg["to"]
    if kind == "accept" and frm == to:
        raise ForgedAccept(
            f"agent {frm} cannot accept an offer that unblocks itself"
        )
    now = time.time()
    with state.locked():
        data = state.prune(state.load(), now)
        row = data["agents"].get(to)
        if row is None:
            return
        for c in row.get("conflicts", []):
            if c.get("ref") != ref:
                continue
            if kind == "accept":
                c["status"] = "resolved"
                row.setdefault("resolved", []).append({
                    "ref": ref, "with": frm, "agreement": msg["msg"],
                })
            elif kind == "refuse":
                c["status"] = "negotiating"
                c["round"] = c.get("round", 0) + 1
            else:
                c["status"] = "negotiating"
        state.save(data)


def mark_waiting(agent_id, ref, recipient_inv):
    """Called after an offer is sent, so silence can be measured."""
    now = time.time()
    with state.locked():
        data = state.prune(state.load(), now)
        for c in data["agents"].get(agent_id, {}).get("conflicts", []):
            if c.get("ref") == ref:
                c["waiting_since_inv"] = recipient_inv
                c["nudged"] = False
        state.save(data)
