# agentmesh — Design

**Date:** 2026-08-28
**Status:** Approved design, pre-implementation
**Working name:** `agentmesh` (placeholder, rename before first release)

## Problem

Three to seven AI coding agents run concurrently against one repository, each
in its own session with no knowledge of the others. They collide. File-level
collisions are the visible symptom; the expensive failures are architectural:
Agent A removes `getUserFromSession` while Agent B is building on it. Both
sessions look successful in isolation and the repo is broken.

Existing mitigations are insufficient:

- **Manual announcement** (agents messaging each other) depends on remembering
  to announce.
- **Git worktrees** eliminate file collisions but also eliminate collaboration:
  agents cannot see each other's in-flight work, which is exactly what related
  changes need.
- **File locks** catch `FILE_OVERLAP` and nothing else. They cannot see that a
  removal in one file breaks a dependency in another.

## Goals

1. An agent knows what every other agent is doing — stated goal, planned
   interface changes, files touched.
2. Interface-level conflicts are detected before either agent commits.
3. A dependent agent can object ("I depend on what you are removing") and the
   two negotiate without a human.
4. A human decides only on deadlock or repo-wide architectural change.
5. Zero infrastructure: no daemon, no service, no dependency beyond `git`
   itself (already present in any repo this runs on).

## Non-goals (v1)

- Symbol-level co-editing of a single file. Claims stay file-granular.
- Semantic duplicate-work detection. Comparing two goals for overlap needs an
  LLM call; `GOAL_OVERLAP` stays a heuristic warning.
- Multi-machine or multi-human coordination.
- Any security boundary between agents. See Trust Model.

## Approach

State lives in a directory in the repo. A single `PreToolUse` hook is the
enforcement engine. Hooks are synchronous and blocking — the only true
enforcement point in an agent loop — and they already exist. A daemon would add
a process without adding power.

The core is tool-agnostic and takes plain dicts. Claude Code is the first
adapter; MCP and git adapters follow without a rewrite.

## Data Model

### Where state lives

State lives at `$(git rev-parse --git-common-dir)/agentmesh/` — inside `.git`,
not at the working-tree root:

```
<git-common-dir>/agentmesh/
  declarations.json     # every live agent, upserted by id
  inbox/<id>.jsonl      # pending messages, drained on read
  refcache.json         # symbol -> reference paths, keyed by HEAD sha
  lock                  # flock target
```

This one choice covers both setups with no mode flag, because git already
guarantees the property needed: **every worktree of a repo shares one
`--git-common-dir`, while each has its own `--show-toplevel`.** Verified:

```
$ cd /repo            && git rev-parse --git-common-dir   # /repo/.git
$ cd /elsewhere/wt-a  && git rev-parse --git-common-dir   # /repo/.git
$ cd /elsewhere/wt-a  && git rev-parse --show-toplevel    # /elsewhere/wt-a
```

So agents sharing one checkout and agents in separate worktrees read and write
the same coordination state, automatically. Consequences:

- No `mode: shared | isolated` config, and no two code paths to test.
- No `.gitignore` entry to patch — `.git/` is never tracked.
- State survives `git clean -xfd`, which an agent may well run.

Because a worktree gives real file isolation, declarations record the
`worktree` (the agent's `--show-toplevel`) and the conflict rules split on it:

| Class | Same worktree | Different worktrees |
|---|---|---|
| `FILE_OVERLAP` | conflict | **not** a conflict — the files are genuinely separate |
| `ADD_COLLISION` (same new file) | conflict | not a conflict |
| `INTERFACE_BREAK` | conflict | **still a conflict** — it surfaces at merge, which is too late |
| `ADD_COLLISION` (same symbol) | conflict | conflict |
| `GOD_SYMBOL` | escalate | escalate |

That is the correct semantics for both of your setups: collaborative agents in
one tree are kept off each other's files, while agents in separate worktrees are
left alone on files and still warned about architectural collisions.

One declaration per agent:

```json
{
  "id": "a3f9c1",
  "goal": "migrate auth from sessions to JWT",
  "globs": ["src/auth/**", "src/middleware/session.ts"],
  "deltas": [
    {"kind": "remove", "symbol": "getUserFromSession"},
    {"kind": "sig",    "symbol": "parseToken"},
    {"kind": "add",    "symbol": "verifyJwt"}
  ],
  "deps": ["createSession", "AuthContext"],
  "touched": ["src/auth/jwt.ts"],
  "progress": "session lookup replaced by verifyJwt(token); call sites now pass a raw token, not a req",
  "worktree": "/Users/me/repo",
  "conflicts": [],
  "resolved": [],
  "state": "active",
  "hb": 1756400000
}
```

`id` is the session id truncated to 6 characters.

Every field has a named reader:

| Field | Read by |
|---|---|
| `goal` | shown to a blocked agent so it can route around intelligently |
| `globs` | cheap first-pass overlap filter, evaluated before any grep |
| `deltas` | interface-conflict detection — the reason this is not a lock table |
| `deps` | forward-looking dependencies grep cannot see (code not yet written) |
| `touched` | overlap detection; not derivable, since a shared tree's `git diff` cannot be attributed per agent |
| `progress` | what the agent has actually produced so far — read by any agent that must adapt to it |
| `worktree` | splits file-level conflicts from interface-level ones (see above) |
| `conflicts` | computed at declare, consumed by the gate |
| `resolved` | agreements that open the gate |
| `state` | prune, and roster display |
| `hb` | absent-agent detection |

`hb` is refreshed on every `PreToolUse` and `PostToolUse` invocation. An agent
that is thinking, or waiting on a long build, still heartbeats as long as it
makes tool calls; one whose process died stops immediately.

The two timers serve different purposes and deliberately differ: 5 minutes
marks an agent **absent** for negotiation, 30 minutes prunes its row. A row
therefore outlives its absence, which is what lets a returning agent still find
the note left in its inbox.

`progress` is the output half of "what is the other agent doing". `goal` and
`touched` together say *intent* and *where*; neither says what came out. An
agent told "A is migrating auth, touched jwt.ts" cannot adapt. One told "call
sites now pass a raw token, not a req" can. It is a single string, overwritten
on every `declare` — current state only, no history, consistent with the rest of
the model.

### Storage discipline

- `declarations.json` is **upsert by id**. Only current state is ever read, so
  nothing is appended.
- Rows pruned when `state == "done"` or `hb` older than TTL (default 30 min).
  Pruning happens in the hook's own write path, not on a schedule.
- `inbox/<id>.jsonl` is the only append-shaped store, and it is truncated the
  moment it is drained.
- `refcache.json` is keyed `<symbol>@<HEAD sha>` and dropped wholesale when
  `HEAD` moves. Bounded by construction; no prune job.
- No history table. Nothing reads history — a resolved negotiation mutates the
  declarations, and those are the only state anyone consults.

At seven agents the whole directory is single-digit KB, so one `flock`'d
read/modify/write beats SQLite.

## Hook Lifecycle

One binary, two faces: `agentmesh hook` (invoked by hooks, reads JSON on stdin)
and the human/agent-facing subcommands.

| Hook | Matcher | Job | Blocking |
|---|---|---|---|
| `SessionStart` | — | prune stale rows, inject roster + "declare before editing" | no |
| `PreToolUse` | `Edit\|Write\|NotebookEdit\|Bash` | the gate | yes |
| `PostToolUse` | same | append to `touched` | no |
| `SessionEnd` | — | `state: done`, release claims | no |

`Stop` is deliberately **not** registered. It fires each time the agent finishes
a response, not when the session ends, so releasing claims there would drop
every agent's claims several times an hour and silently dissolve the
coordination mid-work. `SessionEnd` alone releases; liveness in between comes
from the heartbeat.

### The Bash hole

An `Edit`-only matcher is bypassed by `sed -i`, `cat > file`, `tee`, `mv`.
Agents write files through Bash routinely, so `Bash` is matched and the command
string is regex-scanned for write shapes: `>`, `>>`, `sed -i`, `tee`, `mv`,
`cp`, `rm`, `git apply|checkout|restore`, `patch`. Target paths are extracted
and treated as writes.

Known ceiling: regex over shell strings misses writes inside invoked scripts
and `python -c`. Upgrade path is an mtime-diffing filesystem watcher. This
means enforcement is strong-by-default, not airtight; a determined agent can
route around it. The degraded mode is advisory, which still beats the status
quo. This must be stated in the README.

### Path through the gate

1. **No declaration** → exit 2. stderr instructs the agent to run
   `agentmesh declare`. `SessionStart` already nudged, so this rarely fires.
2. **Declared, file in own globs, unclaimed** → exit 0. Inbox drained into
   `additionalContext` — this is how another agent's objection arrives.
3. **File claimed by another agent** → exit 2 naming that agent's `id`, `goal`,
   and hold duration.
4. **Interface conflict** → exit 2 to the writer, and a message queued to the
   dependent agent, so both sides know before either commits.

### Detection runs at declare, not at the gate

`git grep` across a large repo on every write would make editing slow.
`declare` does the expensive work once — resolve globs, grep each `remove`/`sig`
symbol, cross-reference other declarations — and stores the result in
`conflicts`. The gate is then one `flock`'d JSON read plus a glob match:
sub-50ms, no subprocess.

Consequence: an agent that declares early and drifts is caught only when it
re-declares or touches an unlisted file, which itself blocks and prompts a
re-declare. `declare` is idempotent and cheap to re-run; that is the intended
pressure valve.

## Detection Rules

| Class | Trigger | Action |
|---|---|---|
| `FILE_OVERLAP` | another agent's `globs` ∪ `touched` intersects mine | block writer |
| `INTERFACE_BREAK` | my `remove`/`sig` symbol appears in another agent's claimed files, or in its `deps` | block writer + notify dependent |
| `ADD_COLLISION` | two agents `add` the same symbol, or create the same new file | block later declarer |
| `GOD_SYMBOL` | `remove`/`sig` on a symbol with more than 200 references | escalate to human, skip negotiation |
| `GOAL_OVERLAP` | same globs, no deltas on either side | warn only, never block |

`sig` is treated identically to `remove`: a changed signature breaks callers the
same way, and a separate code path buys nothing.

`GOAL_OVERLAP` is advisory by design. Two agents reading the same area is
normal, and blocking it would make the tool unusable.

### The rule that makes grep viable

**An unclaimed call site is not a conflict.** If A removes
`getUserFromSession` and 40 files reference it, updating those 40 files is A's
job — ordinary work. A conflict requires contention: another agent standing in
the same place.

Grep output is therefore filtered to
`hits ∩ (other agents' globs ∪ touched ∪ deps)` before anything is reported. A
false positive must clear two bars at once — be a bad match *and* land inside
another live declaration. That is rare enough to need no AST.

Remaining noise controls, cheapest first:

- Word-boundary matching, source extensions only, `.gitignore` respected.
- An agent's own globs excluded from its own hits.
- **Short-symbol guard:** a symbol shorter than 4 characters is rejected at
  declare with a demand to qualify it as `path/file.ts:symbol`, which restricts
  the grep to that file. Stops `run`, `get`, `data` from matching the world.
  This guard is about *ambiguity only* and never looks at hit counts — hit
  counts belong to `GOD_SYMBOL`, and having both rules read the same number
  would make one symbol both "rejected" and "escalated".

Known ceiling: `git grep` with word boundaries, no AST. Upgrade path is a
tree-sitter resolver per language, and only if intersect-filtered noise proves
real in practice.

`refcache.json` can go stale mid-session against uncommitted edits in a shared
dirty tree. Accepted: a stale hit still has to intersect a live declaration to
surface.

## Negotiation Protocol

One JSON line per inbox append:

```json
{"from":"a3f9c1","to":"b7d2e0","kind":"objection","ref":"getUserFromSession",
 "msg":"removing this; JWT path replaces it. Can you migrate to verifyJwt?",
 "round":1,"ts":1756400000}
```

Four verbs, each with one sender and one meaning:

| `kind` | Sent by | Meaning |
|---|---|---|
| `objection` | the **dependent** agent (B) | "I rely on `ref`; do not remove it as-is" |
| `offer` | the **blocked** agent (A) | a concrete way forward — a shim, a migration, a different approach |
| `accept` | either, in reply to an `offer` | agreed; the offer's terms are recorded in `resolved` |
| `refuse` | either, in reply to an `offer` | rejected, with a counter-offer in `msg` |

The asymmetry matters: A is the one blocked, so A always carries the burden of
proposing. B can only object or answer. Without this, both sides wait for the
other to move.

### Which conflicts negotiate

`INTERFACE_BREAK`, `FILE_OVERLAP` and `ADD_COLLISION` all use the protocol
below, differing only in `ref`: a symbol for the first and third, a path for
`FILE_OVERLAP`. `GOD_SYMBOL` skips negotiation entirely and escalates.
`GOAL_OVERLAP` never blocks, so it never negotiates.

### Flow (`INTERFACE_BREAK`)

1. A declares `remove:getUserFromSession`. `declare` succeeds — it never fails
   on conflict — and records the conflict in A's `conflicts`. A is blocked at
   its next write, when the gate reads that entry.
2. A's stderr names B's `id`, B's `goal`, and the available moves: propose a
   shim, ask B to migrate, or change approach. A sends an `offer`.
3. The message lands in B's inbox and reaches B as `additionalContext` on B's
   next tool call.
4. B replies `accept`, or `refuse` with a counter-offer.
5. On `accept`, A's declaration gains
   `resolved: [{ref, with: "b7d2e0", agreement: "..."}]` and the gate opens
   for A.

An `accept` is honoured only when `from` is not the blocked agent. Otherwise an
agent unblocks itself in one line.

### Rounds

Cap of 2 exchanges. Past two rounds agents restate rather than converge.
Escalate.

### Deadlock and the absent agent

The failure mode that would kill this tool is an agent blocked forever on one
that is idle, crashed, or whose human left. Two independent timers:

- **Heartbeat stale > 5 min** → the other agent is absent. The blocked agent
  proceeds immediately, and a note describing what changed and why is left in
  the absent agent's inbox. A dead session never holds the repo hostage.
- **Active but silent** → each queued message records the recipient's hook
  invocation count at send time. Once the recipient has passed the gate twice
  more without replying, one nudge is queued; after two further invocations,
  escalate. Counting the recipient's own activity, rather than wall-clock time,
  avoids penalising an agent mid-build.

### Escalation

A `DECISION NEEDED` block naming both goals, the symbol, and each side's
position. Also surfaced by `agentmesh status`. The human answers:

```
agentmesh resolve --ref getUserFromSession \
  --decision "keep a deprecated shim for one release"
```

Written once, unblocking both sides.

`--ref` is matched globally, so a verdict on a symbol applies to every pair
contending on it. Intended: an architectural decision about `getUserFromSession`
should not need restating per agent pair.

### Deterministic fallback

If rounds are exhausted and no human answers, **the later declarer yields** —
first claim wins by timestamp. Arbitrary, but predictable beats a stall, and it
is the one rule an agent can reason about in advance.

## Trust Model

Every agent runs as the same OS user with write access to the state directory.
Any
agent could forge an `accept` from another id. There is no security boundary
here and the design does not pretend to one: `flock` protects against
concurrent corruption, not against a misbehaving agent.

Real isolation would require per-agent credentials and a daemon owning the
state. This must be stated in the README so nobody deploys `agentmesh` as a
multi-tenant safety mechanism.

## Packaging

**Python, single package, stdlib only.** Hook startup around 40ms is invisible
next to an LLM turn, and hackability matters more than milliseconds for a
project that depends on contributions. Go saves roughly 35ms and costs every
contributor a toolchain — reconsider only if profiling demands it.

**No dependency beyond `git`.** `git grep` is always present in a git repo, respects
tracked files, and is fast enough. `rg` is used opportunistically when found on
`PATH`.

### CLI

```
agentmesh init                 # write hook config into .claude/settings*.json
agentmesh declare --goal ... --globs ... [--removes S] [--sig S] [--deps S]
                  [--progress "what I have produced so far"]
agentmesh status               # roster, conflicts, pending decisions
agentmesh reply --to ID --kind offer|accept|refuse --ref S --msg ...
agentmesh resolve --ref S --decision ...
agentmesh release              # drop claims, state: done
agentmesh hook                 # internal; reads hook JSON on stdin
```

`init` is the entire onboarding: it patches the project's
`.claude/settings.json` with the three hook entries. No `.gitignore` edit is
needed, since state lives inside `.git/`. Because those settings live in the
repo, every agent any teammate points at that repo is coordinated
automatically. That property is the adoption story — nobody configures seven
sessions by hand.

### Layout

```
agentmesh/
  cli.py        # arg parsing, command dispatch
  state.py      # flock'd read/write, upsert, prune
  detect.py     # the 5 conflict classes
  negotiate.py  # messages, rounds, timers, escalation
  hooks.py      # payload parsing, Bash write-shape regex, exit codes
tests/
README.md  LICENSE
```

Five modules split along the boundaries the design already has. `state.py` is
the only module that touches disk; every other module takes and returns plain
dicts, which is what makes them testable without a filesystem.

### License

Apache-2.0 — patent grant, and the license teams accept in front of a corporate
repo.

## Testing

Real `pytest`, not a self-check: other people's repos depend on this, and the
concurrency is genuinely hard.

- **Race:** N processes calling `declare` simultaneously; assert no lost writes
  and no corrupt JSON. This is where a file-backed design actually fails, so it
  is written first.
- One test per conflict class, including `GOD_SYMBOL`, and one asserting the
  intersect filter suppresses an unclaimed call site.
- **Absent agent:** a stale heartbeat lets the blocked agent through, and the
  note lands in the absent agent's inbox.
- **Forged accept:** `from == blocked agent` is rejected.
- **Bash write shapes:** table-driven over `sed -i`, `cat >`, `tee`, `mv`, and
  heredocs, plus negatives (`grep`, `cat` with no redirect) so the gate never
  blocks a read.
- **`Stop` does not release:** a simulated end-of-turn leaves claims intact.
  Guards the regression this spec's first draft contained.
- **Worktree split:** two agents in different worktrees editing the same path
  produce no `FILE_OVERLAP`, while a `remove` in one still raises
  `INTERFACE_BREAK` against the other.
- **Common-dir resolution:** state path resolves identically from the main
  checkout and from a linked worktree.
- **Three-agent integration:** subprocesses driven by real hook payloads
  through a full remove → objection → accept cycle. The only test that proves
  the product works.

## Open Questions

None blocking implementation. Deferred by choice: the tool-agnostic MCP
adapter, the tree-sitter reference resolver, and the final project name.
