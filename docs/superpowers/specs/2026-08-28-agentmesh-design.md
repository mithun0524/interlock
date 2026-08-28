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
5. Zero infrastructure: no daemon, no service, no external binaries.

## Non-goals (v1)

- Symbol-level co-editing of a single file. Claims stay file-granular.
- Worktree/isolated mode. That is `git worktree` plus a documentation page,
  not code.
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

Repo root, gitignored:

```
.agentmesh/
  declarations.json     # every live agent, upserted by id
  inbox/<id>.jsonl      # pending messages, drained on read
  refcache.json         # symbol -> reference paths, keyed by HEAD sha
  lock                  # flock target
```

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
| `SessionEnd` / `Stop` | — | `state: done`, release claims | no |

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
- **Generic-symbol guard:** a symbol shorter than 4 characters, or one whose hit
  count exceeds the cap, is rejected at declare with a demand to qualify it as
  `path/file.ts:symbol`. Stops `run`, `get`, `data` from matching the world.
- **`GOD_SYMBOL` short-circuit:** past ~200 references, negotiating with six
  agents is worse than one human decision.

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

`kind` is one of `objection`, `offer`, `accept`, `refuse`. Four verbs cover
every exchange the design requires.

### Which conflicts negotiate

`INTERFACE_BREAK`, `FILE_OVERLAP` and `ADD_COLLISION` all use the protocol
below, differing only in `ref`: a symbol for the first and third, a path for
`FILE_OVERLAP`. `GOD_SYMBOL` skips negotiation entirely and escalates.
`GOAL_OVERLAP` never blocks, so it never negotiates.

### Flow (`INTERFACE_BREAK`)

1. A declares `remove:getUserFromSession`. Detection finds B depends on it. A is
   blocked.
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

### Deterministic fallback

If rounds are exhausted and no human answers, **the later declarer yields** —
first claim wins by timestamp. Arbitrary, but predictable beats a stall, and it
is the one rule an agent can reason about in advance.

## Trust Model

Every agent runs as the same OS user with write access to `.agentmesh/`. Any
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

**No external binaries.** `git grep` is always present in a git repo, respects
tracked files, and is fast enough. `rg` is used opportunistically when found on
`PATH`.

### CLI

```
agentmesh init                 # write hook config + .gitignore entry
agentmesh declare --goal ... --globs ... [--removes S] [--sig S] [--deps S]
agentmesh status               # roster, conflicts, pending decisions
agentmesh reply --to ID --kind offer|accept|refuse --ref S --msg ...
agentmesh resolve --ref S --decision ...
agentmesh release              # drop claims, state: done
agentmesh hook                 # internal; reads hook JSON on stdin
```

`init` is the entire onboarding: it patches the project's
`.claude/settings.json` with the four hook entries and appends `.agentmesh/` to
`.gitignore`. Because those settings live in the repo, every agent any
teammate points at that repo is coordinated automatically. That property is the
adoption story — nobody configures seven sessions by hand.

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
- **Three-agent integration:** subprocesses driven by real hook payloads
  through a full remove → objection → accept cycle. The only test that proves
  the product works.

## Open Questions

None blocking implementation. Deferred by choice: the tool-agnostic MCP
adapter, the tree-sitter reference resolver, and the final project name.
