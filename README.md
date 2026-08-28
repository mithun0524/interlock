# agentmesh

Coordination for 3-7 AI coding agents working the same repository.

## The problem

Run several coding agents on one repo and they collide. File-level collisions
are the visible symptom and the cheap one — you notice immediately.

The expensive failure is architectural. Agent A removes `getUserFromSession`
while Agent B is building on it. Both sessions report success. Nothing looks
wrong until the repo is broken. A lock table cannot catch this: the removal and
the dependency live in different files.

agentmesh makes agents declare what they are doing, detects that class of
conflict before either agent commits, and lets the two settle it without you.

## Install

```bash
pip install agentmesh
cd your-repo
agentmesh init          # --local to install for yourself, not everyone who clones
```

That is the whole setup. `init` writes four hook entries into
`.claude/settings.json`, so every agent pointed at this repo is coordinated
without configuring each session by hand.

## How it works

- **Agents declare intent.** A goal in one sentence, the files they will touch,
  the symbols they will remove or change, what they depend on, and what they
  have produced so far.
- **A blocking `PreToolUse` hook is the gate.** Hooks are synchronous, which
  makes them the only real enforcement point in an agent loop. `Bash` is matched
  alongside `Edit`/`Write`, because `sed -i` and `cat > file` walk straight past
  an `Edit`-only matcher.
- **`git grep` finds who depends on what.** An unclaimed call site is not a
  conflict — updating 40 of them is ordinary work. A conflict requires
  contention: another agent standing in the same place.
- **Agents negotiate; only deadlocks reach you.** The dependent agent objects,
  the blocked agent proposes a way forward, and one of them adapts. You are
  asked only when they cannot agree, or when a change is repo-wide.

## Worktrees

State lives in `<git-common-dir>/agentmesh`, and every worktree of a repo shares
one common dir. So agents in one checkout and agents in separate worktrees
coordinate automatically, with no mode to configure.

The split is deliberate: **files are isolated per worktree, interfaces are not.**
Two agents editing the same path in different worktrees is fine — those really
are different files. One removing a symbol the other depends on is still a
conflict, because that surfaces at merge, which is too late.

## Commands

```
agentmesh init                 # install the hooks into this repo
agentmesh declare --goal ... --globs ... [--removes SYM] [--sig SYM]
                  [--adds SYM] [--deps SYM] [--progress "..."]
agentmesh status [--json]      # roster, conflicts, pending decisions
agentmesh reply --to ID --kind objection|offer|accept|refuse --ref S --msg ...
agentmesh resolve --ref S --decision ...     # your verdict on a deadlock
agentmesh release              # drop claims
agentmesh hook --event E       # internal; reads hook JSON on stdin
```

## Limitations

Read these before trusting agentmesh with anything that matters.

- **Enforcement is strong-by-default, not airtight.** The Bash scanner is regex
  over shell strings; it misses writes inside invoked scripts and `python -c`. A
  determined agent can route around the gate. The degraded mode is advisory,
  which still beats no coordination.
- **Reference detection is `git grep` with word boundaries, not an AST.** False
  positives are suppressed by requiring an intersection with another live
  declaration — reduced, not eliminated. Untracked-but-not-ignored files are
  searched; ignored paths are not.
- **Agents escalate rather than negotiate, by default.** In live runs a blocked
  agent read the hint, understood that `agentmesh reply` existed, and still
  stopped to ask its human instead. The negotiation machinery works, but do not
  expect it to be used unprompted — tell your agents in `CLAUDE.md`/`AGENTS.md`
  that replying is the expected move.
- **No security boundary.** Every agent runs as the same OS user with write
  access to the state directory, and could forge an `accept` from another id.
  `flock` protects against concurrent corruption, not against a misbehaving
  agent. Do not deploy agentmesh as a multi-tenant safety mechanism.
- **Protection covers concurrently-live agents only.** `SessionEnd` releases an
  agent's claims, but the files it wrote stay behind. Observed twice in live
  runs: an agent finished writing docs, exited, and a later agent deleted the
  function those docs described with no conflict raised — correctly, since
  nobody was left to object. Outliving the session would mean keeping a
  dependency record after exit, which cuts against the storage discipline the
  rest of the design rests on. Not planned; know that it is the shape of the
  gap.
- **Temporal overlap matters as much as file overlap, and is not modelled.**
  Two agents conflict only if they are live at the same time. Short tasks
  finish before long ones reach them, so agentmesh does nothing — which looks
  like success and is not. If you are evaluating it, make sure your agents
  actually overlap before concluding anything.
- **`accept` is binary; agents answer in conditions.** A live run produced the
  accept *"go ahead once I confirm tests pass — will ping shortly."* agentmesh
  recorded it as unconditional, opened the gate at once, and no ping ever came.
  The outcome happened to be fine. Treat a recorded agreement as "the other
  agent did not object", not as "the precondition it named was met".
- **Coordination costs turns.** Each negotiation round is tool calls an agent
  spends not writing code. In one live run a 20-turn agent negotiated
  correctly, then hit its cap mid-refactor and left a call site pointing at a
  deleted function — the exact breakage the tool exists to prevent. Give agents
  headroom, or the coordination overhead becomes the failure.
- **Symbol-level co-editing of one file is out of scope.** Claims are
  file-granular.
- **Duplicate-work detection is a heuristic warning, never a block.** Two agents
  reading the same area is normal.

## Design

`docs/superpowers/specs/2026-08-28-agentmesh-design.md` carries the full design
and the reasoning behind each threshold.

## License

Apache-2.0
