---
name: exterminator
description: Fixes bugs at the root cause instead of patching the symptom, then hunts down every other place the same bug lives in the codebase. Use this whenever the user reports a bug, error, crash, exception, stack trace, traceback, failing or red test, regression, or wrong output, or says something is broken, not working, or used to work, even if they only paste an error message, and especially in vibe-coded or AI-generated projects. It reproduces the bug first, traces the real cause, finds and verifies sibling bugs, fixes the shared source, adds a regression test, refuses to cheat by weakening tests or swallowing errors, and explains the cause in plain language. Not for typo fixes, formatting, lint-only cleanup, renames, copy or text edits, or dependency upgrades that are not fixing a failure.
---

# Exterminator

The user saw one bug. There is usually a nest.

Agents tend to patch the line that threw and move on. But the real cause is usually a wrong assumption, a bad pattern, or copy-pasted logic, and it tends to live in several places at once. Studies of real projects back this up: roughly a quarter of resolved bugs needed more than one fix attempt, and the follow-up fixes usually landed in *different* code from the first patch. The people using this skill often don't read their code closely, so if you don't find the nest and explain it, nobody will — and the same bug shows up next week wearing a different stack trace.

Your job: reproduce the bug, find what actually caused it, find every sibling, fix the source, lock it down with a test, and explain it so the user learns something.

## Hard rules

These exist because the most common way agents "fix" bugs is by making the symptom disappear while the bug stays. The user can't tell the difference — that's exactly the problem.

1. **Never hide a bug.** Don't swallow exceptions, add empty catches, add ignore/suppress pragmas, loosen type or lint settings, special-case test inputs, hardcode expected outputs, or weaken, skip, or delete tests. A fix that makes a test pass without making the code correct is worse than no fix, because now the test lies too.
2. **If a test contradicts the intended behavior, stop.** Don't make it pass. Report the conflict (what the test expects, what the code/spec says) and ask which is right.
3. **Stay in scope.** Unrelated problems you notice go in the report under "Also noticed". Don't fix them unless asked.
4. **Ask before big moves.** Refactors, multi-file changes, changing a shared function's signature or contract, and new dependencies need the user's OK. If nobody can answer (a headless or autonomous run), fix only the reported bug plus small same-file siblings, and list everything else under "needs your OK" with the exact change you'd make.
5. **Never install a package just because the code imports it.** AI-written code invents package names, and attackers register those names. Check the name exists in the registry, is the package the code actually meant, and is the one the project already depends on. See `references/bug-classes.md#hallucinated-api-or-package`.

## Step 0: Pick a tier

Start at the lowest tier that fits. Escalate the moment the cause turns out to be a shared helper, a copied block, or an assumption other code also makes.

| Tier | When | What you do |
|---|---|---|
| **T0 trivial** | Typo in a string or name, cosmetic, a value the user told you to change | Fix it. One-line report. No sibling hunt, no log entry. |
| **T1 local** | Wrong logic confined to one function, nothing else depends on the broken assumption | Reproduce → short cause chain → check callers and copies of that one function → regression test → short report → log. |
| **T2 systemic** | Root cause is an assumption, shared helper, copied logic, data shape, state, async/timing, env/config, security, or a hallucinated API/package | The full workflow below, plus a class-level guard. |

Concurrency, security, and data-loss bugs are always T2. If the user explicitly asks for a quick patch only, respect that — but still say "this probably exists elsewhere; I didn't check" so they know.

## Workflow

Copy this checklist into your working notes and tick it off as you go. It keeps you honest when the session gets long.

```
Exterminator:
- [ ] 1. Checked the bug log
- [ ] 2. Reproduced (failing test / script / exact steps) — or stopped and reported
- [ ] 3. Localized with hypotheses (bisected if it used to work)
- [ ] 4. Cause chain: symptom → proximate → root, each link with evidence
- [ ] 5. Named the bug class
- [ ] 6. Hunted siblings: same shape, same function, same assumption
- [ ] 7. Verified every candidate: confirmed / suspected / not a bug
- [ ] 8. Fixed the source (asked first where required)
- [ ] 9. Regression test (fails before, passes after) + class guard
- [ ] 10. audit_fix.py clean, original repro passes unmodified
- [ ] 11. Report written
- [ ] 12. Logged the pattern
```

Scripts referenced below live in this skill's `scripts/` folder (paths are relative to the skill's base directory). They use only the Python 3 standard library; on Windows call `python` instead of `python3`.

### 1. Check the bug log

```bash
python3 scripts/bug_log.py show
```

The project keeps a log of past bug classes at `.claude/bug-patterns.md`, including the exact search that found their siblings. If this bug looks like a logged class, rerun that stored search once you understand the bug (step 6). If the log doesn't exist, that's fine — you'll create it in step 12.

### 2. Reproduce first

Don't edit code until you've watched the bug happen. Without a repro you can't tell a fix from a coincidence.

- Best: a failing test in the project's own test framework. Next: a small script. Last resort: exact steps plus the observed output.
- Make sure it fails **for the reported reason** — the right error, in the right place. A test that fails for some other reason isn't a repro.
- Shrink it: remove inputs and steps until removing anything else makes the bug disappear. Smaller repros make the cause obvious.
- If you genuinely can't reproduce it, **stop**. Report what you tried, what you saw, and what information would help (exact input, environment, logs). Don't change code on a guess. Temporary diagnostic logging is fine; remove it before you finish.

Intermittent bugs: run the repro in a loop, record seeds/timings, and see `references/debugging.md#flaky-and-timing-bugs`.

### 3. Localize with hypotheses

Debug like a scientist, not by trial and error. Each round:

1. **Hypothesis** — "the order is read before it's saved"
2. **Prediction** — "if so, logging both shows the read first"
3. **Experiment** — add the log, run the repro
4. **Conclusion** — confirmed / rejected; next hypothesis

Keep a short running log of hypotheses and results; it becomes the evidence in your cause chain. If the user says "it used to work" and there's git history, bisect instead of guessing: `references/debugging.md#bisecting-regressions`.

### 4. Trace the root cause

Write the chain. Every "because" needs evidence you actually observed (a log line, a test result, a value you printed) — not a plausible story.

```
Symptom:   checkout shows "order not found" right after paying
Proximate: getOrder() runs before saveOrder() has written the row
           (evidence: debug log order — read at 12ms, write at 31ms)
Root:      saveOrder() is async but callers treat it as synchronous;
           nothing awaits it (evidence: no await at checkout.js:14)
```

Stop when you reach something **fixable at the source**: a faulty assumption, a broken contract, a bad pattern, a copied block. That's usually one or two steps past the line that threw. Don't stop at the crash site, and don't keep going into "why did the developer write it this way". If several causes combine, list each one.

### 5. Name the bug class

Pick the class from `references/bug-classes.md` and write the broken assumption in one line ("callers assume `saveOrder` finishes before the next line"). The class name is what makes the sibling search possible and what goes in the log. If nothing fits, coin a short kebab-case name.

### 6. Hunt for siblings

T1: callers and copies of the one function. T2: all three searches. Bugs come in families, and they don't always look alike — follow-up fixes often land in code that doesn't resemble the first one, so a single text search isn't enough.

1. **Same shape** — the syntax pattern of the bug (e.g. an async call with no `await`). Use an AST pattern when the language supports it.
2. **Same function** — every caller of the broken function or helper, and every *copy* of its logic. Copies drift and get renamed, so search for a distinctive line, constant, or string from the body, not just the name.
3. **Same assumption** — other code relying on the thing that turned out to be false: the same field, API response, config key, env var, or contract.

Use the search helper — it picks ast-grep, then Semgrep, then ripgrep, then a pure-Python fallback, and tags how precise each hit is:

```bash
python3 scripts/find_variants.py --lang js --symbol saveOrder
python3 scripts/find_variants.py --lang js --ast 'saveOrder($$$)' --regex 'saveOrder\('
python3 scripts/find_variants.py --lang python --regex '\[.?email.?\]'
```

Stack-specific search recipes and pitfalls: `references/js-ts.md`, `references/python.md`, `references/luau.md`, `references/c-cpp.md`. Read only the one(s) for the languages in play. Also rerun any matching search from the bug log.

If a search returns a flood (more than ~30 hits), narrow the pattern rather than skimming it. To see which search engines and checkers are installed (useful for picking a guard in step 9 too): `python3 scripts/find_variants.py --doctor`.

### 7. Verify every candidate

An unverified match is not a bug — it's a lead. For each candidate, check:

- **Same broken assumption?** Does the thing that was false at the original site also apply here?
- **Reachable?** Can real input or a real code path get here?
- **Already handled?** Is it guarded upstream, awaited by the caller, validated at the boundary?

Prove it with a quick failing test where that's cheap. Then label it: **confirmed** (with evidence), **suspected** (couldn't prove either way — say why), or **not a bug** (with the reason). Traps and a fuller checklist: `references/verifying-variants.md`.

### 8. Fix the class, not just the instance

Prefer fixing the shared source over patching every call site: fix the helper, tighten the type, validate at the boundary (parse API responses once, at the edge), or collapse duplicated logic into one function. Patching N sites is the fallback when there's no shared source.

- Fix the reported bug — that's the request.
- Same-file siblings that need the same small, mechanical fix: fix them too and say so.
- Anything that's a refactor, touches other files, or changes a shared contract: describe the exact change and ask first (hard rule 4).
- Keep diffs minimal. Don't reformat or "improve" surrounding code.

### 9. Guard against regression

- **Regression test for the original bug.** It must fail without your fix and pass with it — actually check this (stash the fix, run, see red, restore). A test that passes either way protects nothing.
- **A guard for the whole class**, where practical: a stricter type, a lint rule (e.g. a no-floating-promises rule), an ast-grep or Semgrep rule saved in the repo, or an assertion at the boundary. The stack references list the usual guard for each class. Adding a new dev dependency or tool to get a guard counts as a "big move" — ask first.

### 10. Self-audit before you report

```bash
python3 scripts/audit_fix.py --repro "<the command that reproduced the bug>"
```

It checks your uncommitted diff (or `--base <ref>`) for the shortcuts in hard rule 1: removed or edited assertions, new skip markers, suppression pragmas, empty catches, swallowed errors, test-only branches, literals copied from test expectations, loosened configs, and new dependencies. FAIL means the original repro still fails: you're not done. For every REVIEW item, either revert it or justify it in the report — some are legitimate (e.g. a test was genuinely wrong *and the user agreed*). Then run the related test suite. The original repro must pass **unmodified**.

### 11. Report

Use this exact template. Plain language: if you use a technical term, gloss it in a few words. The user should come away understanding *why* it broke, not just that it's fixed.

```
Bug: what the user saw
Cause: the real reason, in 1–2 sentences — name the broken assumption
Fixed: where and how
Same bug elsewhere: N found → X fixed · Y need your OK · Z checked, not a bug
  - path:line — fixed / needs your OK / not a bug (why) — one-line note
Prevention: the regression test, plus any class guard added
Verified by: the repro command and test name(s), and the audit result
Lesson: one sentence worth remembering
```

Add "Also noticed:" for out-of-scope issues, and "Couldn't verify:" for suspected siblings. For T0, one line is enough.

Example:

```
Bug: Checkout sometimes said "order not found" right after paying.
Cause: Saving an order takes a moment (it's async), but checkout read the order back without waiting for the save to finish.
Fixed: checkout.js now waits for saveOrder() before loading the order.
Same bug elsewhere: 3 found → 1 fixed · 1 needs your OK · 1 checked, not a bug
  - checkout.js:41 — fixed — same file, same missing await on saveCart()
  - jobs/nightly.js:12 — needs your OK — forEach(async …) never waits; switch to for…of
  - cache.js:8 — not a bug — returns the promise to a caller that awaits it
Prevention: test/checkout.test.js "loads the order it just saved"; enabled no-floating-promises lint rule
Verified by: node --test test/checkout.test.js (failed before, passes now); audit_fix.py PASS
Lesson: If a function is async, every caller has to wait for it — or it's a race you'll lose sometimes.
```

### 12. Log the pattern (T1 and T2)

```bash
python3 scripts/bug_log.py add --cls missing-await --lang js \
  --seed "js/src/checkout.js:14 — order read before save finished" \
  --assumption "saveOrder() is synchronous" \
  --search "python3 scripts/find_variants.py --lang js --symbol saveOrder" \
  --sibling "js/src/checkout.js:41 | fixed" \
  --sibling "js/src/jobs/nightly.js:12 | needs OK | forEach(async)" \
  --sibling "js/src/cache.js:8 | not a bug | returned to awaiting caller" \
  --guard "no-floating-promises lint rule"
```

Record the **search command**, not just the result — the next session reruns it to catch new copies before they ship.

## When to stop and report instead of fixing

- You can't reproduce the bug.
- A test contradicts the intended behavior, or the spec is ambiguous about which is right.
- The only way forward is to disable a test, check, or type rule.
- The root cause is in a dependency or external service — report it and propose a workaround; don't patch `node_modules`/`site-packages`.
- The fix needs secrets, production data, or access you don't have.

Stopping with a clear explanation is a successful outcome. A fake fix is the failure case.

## Reference files

Load only what the current bug needs.

- `references/bug-classes.md` — cross-stack catalog: each class's broken assumption, how to hunt it, how to verify, and the class guard
- `references/debugging.md` — repro shrinking, hypothesis loop, targeted logging, bisecting, flaky/timing bugs, writing the cause chain
- `references/verifying-variants.md` — candidate verification checklist and false-positive traps
- `references/js-ts.md` — JavaScript, TypeScript, React, Next.js
- `references/python.md` — Python
- `references/luau.md` — Luau / Roblox
- `references/c-cpp.md` — C and C++
- `scripts/find_variants.py` — sibling search with automatic tool fallback (`--help`)
- `scripts/audit_fix.py` — anti-cheat self-audit of your diff (`--help`)
- `scripts/bug_log.py` — read/append `.claude/bug-patterns.md` (`--help`)
- `scripts/bisect_repro.py` — wrapper for `git bisect run` (`--help`)
