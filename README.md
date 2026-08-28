<div align="center">

# 🔗 interlock

**Stop your AI coding agents from breaking each other's work.**

Interface-level conflict detection and agent-to-agent negotiation for repos with
several coding agents in flight at once.

[![tests](https://github.com/mithun0524/interlock/actions/workflows/ci.yml/badge.svg)](https://github.com/mithun0524/interlock/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/mithun0524/interlock)
[![license](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)](pyproject.toml)

[Quick start](#quick-start) · [How it works](#how-it-works) · [CLI](#cli) ·
[Configuration](#configuration) · [Limitations](#limitations) ·
[Contributing](#contributing)

</div>

---

## The problem

Run three to seven coding agents on one repository and they collide. File-level
collisions are the visible symptom, and the cheap one — you notice immediately.

The expensive failure is architectural:

> Agent A removes `getUserFromSession`. Agent B is building on it.
> Both sessions report success. Nothing looks wrong until the repo is broken.

A lock table cannot catch that. The removal and the dependency live in different
files, so nothing overlaps. `interlock` makes agents declare what they intend,
detects that class of conflict *before* either agent lands it, and lets the two
settle it without you.

## Quick start

```bash
pip install git+https://github.com/mithun0524/interlock
cd your-repo
interlock init
```

That's the whole setup. `init` writes four hook entries into
`.claude/settings.json`, so every agent pointed at this repo is coordinated —
no per-session configuration. Add `--local` to install for yourself only rather
than everyone who clones.

Requires Python 3.9+. No runtime dependencies; `git` is the only external
binary. Not on PyPI yet.

## See it work

Two agents, one dependency. Agent `one01` owns `b.py`, which calls `helperFn`.
Agent `two02` sets out to delete `helperFn`:

```console
$ interlock declare --goal "delete the helper" --globs "a.py" --removes helperFn
interlock: declared, 1 conflict(s) (1 blocking):
  [INTERFACE_BREAK] helperFn with one01: referenced in b.py, claimed by one01
interlock: 1 objection(s) raised on your behalf by the agents that depend on
this. Answer with `interlock reply --kind offer`.
```

Its next write is blocked — even through `sed -i`, which an `Edit`-only hook
would miss entirely:

```console
$ # agent runs: sed -i '' s/x/y/ a.py
interlock: blocked.
  [INTERFACE_BREAK] helperFn with one01: referenced in b.py, claimed by one01
      their goal: own b.py
Next step: interlock reply --to one01 --kind offer --ref helperFn
           --msg "<a shim, a migration, or a different approach>"
Propose something concrete — you are the blocked party, so the move is yours.
Do not stop here for a human.
```

In live three-agent runs this resolves on its own:

```
objection   one01 → two02   raised by interlock; one01 took no action
offer       two02 → one01   "I will leave a deprecated shim for one release"
accept      one01 → two02   agreed
→ gate opens, repo stays consistent
```

## How it works

| | |
|---|---|
| **Agents declare intent** | A one-sentence goal, the files they'll touch, symbols they'll remove or change, what they depend on, and what they've produced so far. |
| **A blocking `PreToolUse` hook is the gate** | Hooks are synchronous — the only real enforcement point in an agent loop. `Bash` is matched alongside `Edit`/`Write`, because `sed -i` and `cat > file` walk past an `Edit`-only matcher. |
| **`git grep` finds who depends on what** | An unclaimed call site is *not* a conflict — updating 40 of them is ordinary work. A conflict requires contention: another agent standing in the same place. |
| **Agents negotiate; deadlocks reach you** | `interlock` raises the first objection itself, so a negotiation doesn't need two agents to both act. You're asked only on genuine deadlock, or when a change is repo-wide. |

### Conflict classes

| Class | Fires when | Effect |
|---|---|---|
| `INTERFACE_BREAK` | you remove/change a symbol another live agent's files reference or declared as a dependency | blocks, auto-objection raised |
| `FILE_OVERLAP` | another live agent claims the same path *in the same worktree* | blocks |
| `ADD_COLLISION` | two agents add the same symbol, or create the same new file | blocks |
| `GOD_SYMBOL` | a removal touches more than 200 references | escalates to you; too big to negotiate |
| `GOAL_OVERLAP` | same files, neither side changing an interface | warns only, never blocks |

### Worktrees

State lives in `<git-common-dir>/interlock`, and every worktree of a repo shares
one common dir. Agents in a single checkout and agents in separate worktrees
coordinate automatically, with no mode to configure.

The split is deliberate: **files are isolated per worktree, interfaces are not.**
Two agents editing the same path in different worktrees is fine — those really
are different files. One removing a symbol the other depends on is still a
conflict, because it surfaces at merge, which is too late.

## CLI

```bash
interlock init [--local]                    # install the hooks into this repo
interlock declare --goal ... --globs ...    # register or update your intent
         [--removes SYM] [--sig SYM] [--adds SYM] [--deps SYM]
         [--progress "what you've produced"]
interlock status [--json]                   # roster, conflicts, pending decisions
interlock reply --to ID --kind objection|offer|accept|refuse --ref S --msg ...
interlock resolve --ref S --decision ...    # your verdict on a deadlock (human only)
interlock release                           # drop claims
interlock hook --event E                    # internal; reads hook JSON on stdin
```

## Configuration

Thresholds are module constants in `interlock/state.py`:

| Constant | Default | Meaning |
|---|---|---|
| `TTL_ABSENT` | 300s | no heartbeat for this long → treated as absent, blocked agent proceeds |
| `TTL_PRUNE` | 1800s | declaration dropped entirely |
| `GOD_SYMBOL_REFS` | 200 | above this, a removal escalates instead of negotiating |
| `MIN_SYMBOL_LEN` | 4 | shorter bare symbols must be qualified as `path:symbol` |
| `ROUND_CAP` | 2 | negotiation rounds before escalation |

Every default is a considered guess, not a measurement. Tune them to your repo.

| Environment variable | Effect |
|---|---|
| `INTERLOCK_ID` | override the agent id (beats an inherited session id) |
| `INTERLOCK_ALLOW_AGENT_RESOLVE` | let automation run `resolve`, which agent sessions are otherwise refused |

## Limitations

Read these before trusting `interlock` with anything that matters. They come
from running real concurrent agent sessions, not from speculation.

- **Enforcement is strong-by-default, not airtight.** The Bash scanner is regex
  over shell strings; it misses writes inside invoked scripts and `python -c`. A
  determined agent can route around the gate. The degraded mode is advisory,
  which still beats no coordination.
- **Reference detection is `git grep` with word boundaries, not an AST.** False
  positives are suppressed by requiring an intersection with another live
  declaration — reduced, not eliminated. Untracked-but-not-ignored files are
  searched; ignored paths are not.
- **No security boundary.** Every agent runs as the same OS user with write
  access to the state directory, and could forge an `accept` from another id.
  `flock` protects against concurrent corruption, not against a misbehaving
  agent. Do not deploy `interlock` as a multi-tenant safety mechanism.
- **Protection covers concurrently-live agents only.** `SessionEnd` releases an
  agent's claims, but the files it wrote stay behind. Observed twice: an agent
  finished writing docs, exited, and a later agent deleted the function those
  docs described — no conflict raised, correctly, since nobody was left to
  object.
- **Temporal overlap matters as much as file overlap, and isn't modelled.** Two
  agents conflict only while both are live. Short tasks finish before long ones
  reach them, so `interlock` does nothing — which looks like success and isn't.
  If you're evaluating it, make sure your agents actually overlap.
- **`accept` is binary; agents answer in conditions.** A live run produced
  *"go ahead once I confirm tests pass — will ping shortly."* `interlock`
  recorded it as unconditional, opened the gate, and no ping came. Read a
  recorded agreement as "the other agent did not object", not "the precondition
  it named was met".
- **Coordination costs turns.** Each negotiation round is tool calls an agent
  spends not writing code. One 20-turn agent negotiated correctly, then hit its
  cap mid-refactor and left a call site pointing at a deleted function — the
  exact breakage the tool exists to prevent. Give agents headroom.
- **Agents needed prompting before they'd negotiate.** Early runs exchanged zero
  messages: the block listed options and agents read that as licence to stop and
  ask a human. Fixed by giving one imperative next step and raising the first
  objection automatically — but if you change that wording, expect the old
  behaviour back.
- **Symbol-level co-editing of one file is out of scope.** Claims are
  file-granular.
- **Duplicate-work detection is a heuristic warning, never a block.**

## Roadmap

- [ ] Conditional `accept` — a state that keeps blocking until the named
      precondition is confirmed
- [ ] MCP adapter, so Cursor, Codex and other MCP agents work too (the core is
      tool-agnostic and takes plain dicts; this is an adapter, not a rewrite)
- [ ] Tree-sitter reference resolution, if grep noise proves real in practice
- [ ] Threshold defaults backed by measurement instead of judgement
- [ ] PyPI release

## Design

- [Design spec](docs/superpowers/specs/2026-08-28-interlock-design.md) — the full
  design and the reasoning behind every threshold
- [Implementation plan](docs/superpowers/plans/2026-08-28-interlock.md) — 12 TDD
  tasks from skeleton to release

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `state.py` is the only
module that touches disk, every write goes through `flock`, and bugs get a
regression test that says how the bug was found.

## License

[Apache-2.0](LICENSE)
