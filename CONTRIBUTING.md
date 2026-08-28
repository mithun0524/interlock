# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -q
```

Runtime dependencies: **none**, and it stays that way. `git` is the only
external binary. `pytest` is the sole test dependency.

## Ground rules

- **`state.py` is the only module that touches disk.** Everything else takes
  and returns plain dicts, which is what makes it testable without a
  filesystem. Keep it that way.
- **Every write to `declarations.json` goes through `state.locked()`** and lands
  via atomic replace. `tests/test_state_race.py` runs 20 concurrent processes;
  if you change the write path, that test is the one that matters.
- **Upsert, never append.** Only current state is read, so no history is kept.
  If you find yourself wanting a history table, name the reader first.
- **One conflict, one imperative next step.** Block messages must give exactly
  one command. Presenting options was measured to make agents stop and ask a
  human instead of acting — see the Limitations section of the README.
- **Thresholds live in `state.py`** as module constants, nowhere else.

## Tests

Bugs get a regression test that would have caught them, with a comment saying
how the bug was found. Several current tests exist because live agent runs
found things 133 unit tests missed; that provenance is worth keeping.

Live testing matters here. Unit tests cannot tell you whether a real agent
complies with a hook, and every serious defect so far came from running actual
concurrent sessions against a scratch repo.

## Commits

Subject, blank line, body explaining *why*. No AI attribution trailers.
