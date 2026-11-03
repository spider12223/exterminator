# Debugging techniques that actually work for an agent

## Contents
- [Reproducing and shrinking](#reproducing-and-shrinking)
- [The hypothesis loop](#the-hypothesis-loop)
- [Targeted logging](#targeted-logging)
- [Bisecting regressions](#bisecting-regressions)
- [Flaky and timing bugs](#flaky-and-timing-bugs)
- [Environment and config bugs](#environment-and-config-bugs)
- [Writing the cause chain](#writing-the-cause-chain)
- [What not to bother with](#what-not-to-bother-with)

## Reproducing and shrinking

A repro is the contract for the whole fix: it proves the bug exists, and later it proves the fix works.

1. **Find the cheapest trigger.** Prefer, in order: an existing failing test → a new test in the project's framework → a standalone script → exact manual steps. Put new tests where the project keeps its tests and follow their style.
2. **Check it fails for the right reason.** Compare the error message, type, and location with what the user reported. If a test fails on an import error or missing fixture, that's not the bug.
3. **Shrink it** (delta debugging, by hand): repeatedly remove half of the input, steps, or setup; keep the half that still fails. Stop when every remaining piece is needed. A 3-line repro usually points straight at the cause.
4. **Record the repro command.** You'll pass it to `audit_fix.py --repro` and quote it in the report.

If you can't reproduce it: list what you tried (inputs, environment, commands) and what you saw, name the information that would help (exact input, logs, browser/OS/version, timing), and stop. Guessed fixes for unreproduced bugs are how new bugs get written.

## The hypothesis loop

Write each round down; it becomes your evidence.

```
H1: order is read before the save completes
  Predict: log shows READ before WRITE-DONE
  Test:    add EXTERMINATOR-DEBUG logs in saveOrder and getOrder, run repro
  Result:  READ at 12ms, WRITE-DONE at 31ms -> confirmed
```

Rules of thumb:
- One variable per experiment. If you change two things and it works, you don't know why.
- Prefer experiments that can **reject** your favorite hypothesis. Seeking only confirming evidence is how wrong root causes get reported.
- When a hypothesis is rejected, say so and move on — don't bend the evidence.
- Use the debugger or REPL when available (`node --inspect`, `python -m pdb`, `breakpoint()`, `gdb`/`lldb`); otherwise targeted logging.

## Targeted logging

- Tag every temporary log line with `EXTERMINATOR-DEBUG` so you can find and remove them all: `audit_fix.py` flags any that remain.
- Log the **values and the order of events**, not just "got here": `EXTERMINATOR-DEBUG getOrder id=42 found=false t=12ms`.
- Log at both ends of a suspected boundary (producer and consumer) to see where the value goes wrong.
- Remove them before finishing. If a permanent log line is genuinely useful, write it properly (normal logger, normal level) and mention it in the report.

## Bisecting regressions

Use when the user says "it used to work" and git history covers the working version.

1. Find a good commit (e.g. a tag or a date the user remembers: `git log --since=... --oneline`) and confirm the repro passes there.
2. Copy `scripts/bisect_repro.py` somewhere outside the repo if the skill lives inside it (bisect checks out old commits).
3. Make the repro a command that **exits 0 when the bug is absent**. If the test file doesn't exist in old commits, put the repro in a temp file outside the repo.
4. Run:
   ```bash
   git stash -u            # bisect needs a clean tree; remember to pop later
   git bisect start <bad> <good>
   git bisect run python3 /tmp/bisect_repro.py --setup "npm ci --silent" --repro "node /tmp/repro.js" --expect "order not found"
   git bisect reset
   git stash pop
   ```
5. Read the first bad commit's diff. It usually shows the broken assumption directly — and often the same change touched other files (sibling candidates).

`--expect` matters: commits that fail for unrelated reasons get skipped instead of blamed.

## Flaky and timing bugs

- **Run it many times**: `for i in $(seq 50); do <repro> || echo FAIL $i; done`. Record the failure rate before and after the fix — a fix should take it to 0 across a meaningful number of runs, not 1 lucky pass.
- **Fix the seed** for randomness; record seeds that fail.
- **Widen the window**: insert a short delay between the suspected racing operations (in a debug build) to make the race reproduce reliably. If the delay makes it fail every time, you've localized it.
- **Look for shared state**: globals, module-level caches, singletons, the same DB row/file/DataStore key, test order dependence (run the failing test alone vs in the suite).
- Timing fixes are where incorrect fixes are most common. Prefer structural fixes (await, lock, transaction, idempotency) over sleeps/retries. Adding a sleep or retry to make the test pass is hiding the bug.

## Environment and config bugs

When it works in one place and not another:
- Diff the environments: env vars (names, not secret values), runtime versions (`node -v`, `python --version`), dependency lockfiles, OS, feature flags, config files.
- Print the **effective** config at the point of use, not what you think it should be.
- Search every read of the key (see `env-config-drift` in `bug-classes.md`).

## Writing the cause chain

```
Symptom:   what the user saw
Proximate: the immediate technical cause (evidence: ...)
Root:      the assumption/contract/pattern that made it possible (evidence: ...)
```

- Each link needs evidence you observed, not inference.
- Stop at something **fixable at the source**. Too shallow: "line 40 threw". Too deep: "the developer didn't know async". Right: "saveOrder() is async but its callers treat it as synchronous".
- Several root causes are fine; list each. Bugs often need two things to go wrong.
- If the root cause is outside the repo (dependency, platform, external API), say so and fix at your boundary (validation, workaround), not inside the dependency.

## What not to bother with

- **Spectrum-based fault localization** (ranking lines by test coverage) needs a large passing test suite most small projects don't have, and studies found it helps less in practice than on paper. Use it only if the project already produces per-test coverage.
- **Formal fault trees / probability analysis**: overkill for a code bug. A short evidence-backed chain is enough.
- **Asking "why" exactly five times**: the number is arbitrary and it forces a single chain. Stop when you reach the fixable source.
