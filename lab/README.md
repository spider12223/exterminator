# exterminator lab

A test rig for the [`exterminator`](../skills/exterminator) skill. Every task runs headless Claude Code against a fresh copy of a seeded repo, with and without the skill. The scorer then grades the result with checks the agent never sees.

Run everything below from this `lab/` folder.

## What's in it

```
testbed/            ShopKit: a small shop backend in JS, Python, Luau and C plus a static web storefront, with seeded bugs
tasks/tasks.json    the 14 prompts, written the way a vibe coder would type them
grading/
  ground_truth.json where each bug, sibling and look-alike lives
  hidden/           checks the agent never sees: one per bug, sibling and look-alike
  score.py          grades one run, or summarizes a batch
  selftest.py       proves the grader works (golden fixes, cheats, untouched code). No model calls.
run_evals.py        creates the runs, calls `claude -p`, scores each one
trigger_evals.json  24 queries for tuning the skill description (12 should trigger, 12 near-misses)
```

**Bug tasks (S1–S8, V1–V3).** Each has one reported bug, 2–3 hidden siblings in other files (at least one that doesn't look like the original), and 1–2 safe look-alikes. Changing a look-alike counts as a false positive.

| Task | Class | Language |
|---|---|---|
| S1 | missing await (incl. `forEach(async …)`) | JS |
| S2 | unchecked None | Python |
| S3 | off-by-one (1-based pages) | JS |
| S4 | swallowed error | Python |
| S5 | shared mutable default (incl. shallow `table.clone`, `__index` defaults) | Luau |
| S6 | missing NUL terminator (`strncpy`, `memcpy`) | C |
| S7 | hallucinated package (`dateutils`) | Python |
| S8 | env var renamed, some code still reads the old name | JS |
| V1 | layout shift (image without dimensions, banner inserted late) | Web |
| V2 | flash of wrong state (old state removed a frame before the new one lands) | Web |
| V3 | overflow clipping (dropdown, header menu, tooltip in a scroll box) | Web |

**Visual tasks (V1–V3)** live in `testbed/web/`, a static storefront (HTML, CSS, vanilla JS modules, no build step). Their hidden checks serve it on 127.0.0.1 and drive headless Chromium: they hold images and data back to measure layout shift and element rects, record the state the browser renders on every frame, and hit-test menus and tooltips. They never read the source, so any correct fix passes and hiding the symptom fails.

**Behaviour traps.**
- **T1:** the test contradicts the README. The skill should stop and ask, not edit the test or the code.
- **T2:** a report that can't be reproduced. The skill should say so and not change product code.
- **T3:** a typo. The skill should do a one-line fix with no sibling hunt and no log entry.

## Requirements

- Python 3.10+, git, and Claude Code (`claude` on PATH).
- Node 18+ for S1, S3, S8, T1 and T3.
- The `luau` CLI for S5 (github.com/luau-lang/luau releases).
- A C compiler and `make` for S6.
- Python Playwright with Chromium for V1–V3: `python -m pip install playwright`, then `python -m playwright install chromium`. The scorer runs the web checks with its own interpreter, so run `selftest.py` and `run_evals.py` with that Python.

Tasks whose toolchain is missing still run, but their hidden checks are skipped, so nothing they check counts as fixed. The scorer says so under the run (`! hidden checks skipped: <why>`) and in the summary, and `run_evals.py` warns before it starts V tasks without Playwright. Use `--tasks` to leave them out.

With-skill runs on V1–V3 can record the storefront with the skill's `scripts/bugcam.py` (`record web --serve web`), which needs Pillow and Playwright in the Python the agent calls. Baseline runs get the same Python, just not the script.

**Important:** while running baselines, don't have the skill installed, or it leaks into the "without skill" runs. The runner refuses to start if it finds it in `~/.claude/skills/`; it can't see a plugin install, so run `/plugin uninstall exterminator@exterminator` first if you used that route. Each "with skill" run gets the skill as a project skill inside its own copy of the repo.

**About the hidden checks.** They're hidden from the agent under test, not from the world: this repo is public, and `grading/` contains every answer. Don't point a model at this repo while it's being evaluated.

## Run it

```bash
# 0. Check the grader itself (no model calls, ~3 min). Expect 45/45.
#    A case whose toolchain is missing fails and says why: without the `luau` CLI the 3 Luau
#    cases fail ("luau not installed"), without Playwright the 19 V cases ("playwright not installed").
python3 grading/selftest.py

# 1. Small batch first: 3 tasks x (with + without skill) = 6 runs
python3 run_evals.py --tasks S1,S3,T1 --baseline

# 2. Everything: 14 tasks x 2 = 28 runs
python3 run_evals.py --baseline

# Re-print a batch summary
python3 run_evals.py --summary runs/<stamp>
```

These are real model runs billed to your plan, and a bug task can take 20–80 turns. Start with the small batch. Use `--repeat 3 --tasks S5` when you want to see variance on one task.

Runs use `--allowedTools Read,Edit,Write,Glob,Grep,Bash,Skill,TodoWrite` inside throwaway copies. If your Claude Code version handles permissions differently in headless mode, pass extra flags with `--extra-args "--permission-mode acceptEdits"`. Each run lands in `runs/<stamp>/<task>/<config>/run-N/` with `repo/`, `transcript.jsonl` and `score.json`.

## Reading the scores

Per run:
```
S1-missing-await  with seed=Y siblings found 3/3 fixed 1 decoyFP=0 rca=Y log=Y trig=Y flags=0
```
- **seed**: the reported bug is fixed, judged by a hidden check, not the visible test.
- **siblings found / fixed**: "found" means fixed, touched, or named in the final report; "fixed" means its hidden check passes. A sibling the skill reported as "needs your OK" counts as found but not fixed, which is correct behaviour in a headless run.
- **decoyFP**: safe look-alike code that got changed. Should be 0.
- **rca**: the report names the root cause (keyword rubric). Skim the report to confirm.
- **log / trig**: whether the bug log was written and whether the skill loaded.
- **flags**: bad-fix signals: edited assertions, skipped tests, swallowed errors, forbidden dependencies, out-of-scope edits, broken look-alikes.

Lines starting with `?` point at things to read yourself, such as a look-alike that was mentioned (it should be "not a bug") or a sibling that was mentioned but not fixed.

**Targets for calling the skill good:**
- with-skill sibling recall at least 80%
- 0 decoy fixes and 0 bad-fix flags
- all 3 traps pass
- the skill triggers on every S and V task and on none of T3
- a clear gap over the baseline

## Tuning the trigger description

`trigger_evals.json` is in the format skill-creator's optimizer expects. In Claude Code, with the skill-creator skill available, ask it to "optimize the exterminator description using lab/trigger_evals.json". Or run its script directly from the skill-creator folder:

```bash
python -m scripts.run_loop --eval-set /path/to/exterminator/lab/trigger_evals.json \
  --skill-path /path/to/exterminator/skills/exterminator --model <your model id> --max-iterations 5 --verbose
```

It holds out 40% of the queries for testing and picks the description with the best held-out score.

## Iterating

After a batch, the most useful things to look at are the summary plus the transcripts of any run with flags, a missed sibling, or a failed trap. Fix the skill, rerun the same tasks with `--baseline`, and compare.
