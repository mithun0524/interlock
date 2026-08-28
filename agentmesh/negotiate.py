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


def is_absent(row, now):
    return now - row.get("hb", 0) > state.TTL_ABSENT


def check_timers(now=None):
    now = time.time() if now is None else now
    changed, notes, nudges = [], [], []

    with state.locked():
        data = state.prune(state.load(), now)
        agents = data["agents"]
        for aid, row in agents.items():
            for c in row.get("conflicts", []):
                if c.get("status") == "resolved":
                    continue
                other_id = c.get("with")
                other = agents.get(other_id)

                # 1. absent counterparty: never hold the repo hostage
                if c.get("status") != "escalated" and other_id and (
                        other is None or is_absent(other, now)):
                    c["status"] = "resolved"
                    c["detail"] += " | counterparty absent, proceeded"
                    row.setdefault("resolved", []).append({
                        "ref": c["ref"], "with": other_id,
                        "agreement": "counterparty absent; proceeded "
                                     "unilaterally",
                    })
                    if other is not None:
                        notes.append((other_id, aid, c["ref"]))
                    changed.append(c)
                    continue

                # 2. round cap
                if c.get("status") != "escalated" \
                        and c.get("round", 0) > state.ROUND_CAP:
                    c["status"] = "escalated"
                    c["escalated_at_inv"] = row.get("inv", 0)
                    changed.append(c)
                    continue

                # 3. already escalated: the fallback pass handles it
                if c.get("status") == "escalated":
                    continue

                # 4. silence, counted in the recipient's own activity
                waiting = c.get("waiting_since_inv")
                if waiting is not None and other is not None:
                    elapsed = other.get("inv", 0) - waiting
                    if elapsed >= state.ESCALATE_AFTER_INV:
                        c["status"] = "escalated"
                        c["escalated_at_inv"] = row.get("inv", 0)
                        changed.append(c)
                    elif elapsed >= state.NUDGE_AFTER_INV \
                            and not c.get("nudged"):
                        c["nudged"] = True
                        nudges.append((other_id, aid, c["ref"]))
                        changed.append(c)
        state.save(data)

    changed += _apply_fallback(now)

    for to, frm, ref in notes:
        send(frm, to, "objection", ref,
             f"{frm} proceeded with {ref} while you were absent "
             f"(no activity for over {state.TTL_ABSENT}s). "
             f"Re-read the file before continuing.")
    for to, frm, ref in nudges:
        send(frm, to, "objection", ref,
             f"nudge: {frm} is still blocked on {ref} and needs your reply "
             f"(accept or refuse).")
    return changed


def _apply_fallback(now):
    """An escalated conflict the human never answers cannot stall forever.

    Once the escalated agent has passed ESCALATE_AFTER_INV more gate
    invocations with no verdict, first claim wins by timestamp: the later
    declarer yields and is told to adapt, the earlier declarer proceeds.
    Arbitrary, but predictable beats a stall.
    """
    changed, told = [], []
    with state.locked():
        data = state.prune(state.load(), now)
        agents = data["agents"]
        for aid, row in agents.items():
            for c in row.get("conflicts", []):
                if c.get("status") != "escalated":
                    continue
                waited = row.get("inv", 0) - c.get("escalated_at_inv", 0)
                if waited < state.ESCALATE_AFTER_INV:
                    continue
                pair = [x for x in (aid, c.get("with")) if x in agents]
                if len(pair) < 2:
                    continue
                loser = max(pair,
                            key=lambda a: agents[a].get("declared_at", 0))
                winner = pair[0] if pair[1] == loser else pair[1]
                for holder in pair:
                    for cc in agents[holder].get("conflicts", []):
                        if cc.get("ref") != c["ref"]:
                            continue
                        cc["status"] = "resolved"
                        cc["detail"] += (
                            f" | no human verdict; first claim wins, "
                            f"{loser} yields to {winner}"
                        )
                        changed.append(cc)
                    agents[holder].setdefault("resolved", []).append({
                        "ref": c["ref"], "with": "fallback",
                        "agreement": f"first claim wins; {loser} yields "
                                     f"to {winner}",
                    })
                told.append((loser, winner, c["ref"]))
        state.save(data)

    for loser, winner, ref in told:
        send(winner, loser, "objection", ref,
             f"no human verdict arrived, so the deterministic fallback "
             f"applied: first claim wins and you declared later, so you "
             f"yield on {ref}. Adapt to {winner}'s change.")
    return changed


def escalations():
    out = []
    for aid, row in sorted(state.get_all().items()):
        for c in row.get("conflicts", []):
            if c.get("status") == "escalated":
                entry = dict(c)
                entry["agent"] = aid
                out.append(entry)
    return out


def resolve(ref, decision):
    """A verdict on a symbol applies to every pair contending on it — an
    architectural decision should not need restating per agent pair."""
    now = time.time()
    n = 0
    with state.locked():
        data = state.prune(state.load(), now)
        for row in data["agents"].values():
            for c in row.get("conflicts", []):
                if c.get("ref") == ref and c.get("status") != "resolved":
                    c["status"] = "resolved"
                    row.setdefault("resolved", []).append({
                        "ref": ref, "with": "human", "agreement": decision,
                    })
                    n += 1
        state.save(data)
    return n


def yield_later_declarer(ref):
    """Deterministic fallback. Predictable beats a stall."""
    agents = state.get_all()
    involved = set()
    for aid, row in agents.items():
        for c in row.get("conflicts", []):
            if c.get("ref") == ref:
                involved.add(aid)
                if c.get("with"):
                    involved.add(c["with"])
    if not involved:
        return ""
    return max(involved,
               key=lambda a: agents.get(a, {}).get("declared_at", 0))
