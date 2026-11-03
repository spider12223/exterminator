# exterminator

A Claude skill that fixes bugs at the root cause, then hunts down every other place the same bug lives.

Most agents patch the line that threw and move on. The real cause is usually a wrong assumption or a copy-pasted pattern, and it tends to live in several files at once. `exterminator` reproduces the bug, traces the cause, finds the siblings, fixes the source, locks it down with a regression test, and explains it in plain language.

## Install

Pick one. All three put the same skill in Claude Code.

**One command** (needs Node):

```bash
npx skills add spider12223/exterminator
```

**As a Claude Code plugin** (inside Claude Code):

```
/plugin marketplace add spider12223/exterminator
/plugin install exterminator@exterminator
```

**By hand:**

```bash
git clone --depth 1 https://github.com/spider12223/exterminator.git
cp -r exterminator/skills/exterminator ~/.claude/skills/
```

Or paste this to Claude and let it do the work:

> Install the exterminator skill from https://github.com/spider12223/exterminator (the folder is `skills/exterminator`) into my Claude skills.

Then just report a bug the way you normally would: paste the error, say a test is red, or say something stopped working. The skill triggers on its own.

## What it does

```
reproduce  ->  cause chain  ->  name the bug class  ->  hunt siblings
   ->  verify each candidate  ->  fix the source  ->  regression test
   ->  anti-cheat audit  ->  plain-language report  ->  log the pattern
```

It picks the smallest tier that fits and escalates when the cause turns out to be shared:

| Tier | When | Effort |
|---|---|---|
| T0 trivial | typo, cosmetic, a value you told it to change | fix it, one-line report |
| T1 local | wrong logic confined to one function | reproduce, check callers and copies, test, short report |
| T2 systemic | shared helper, copied logic, async, config, security, data loss, hallucinated package | the full workflow plus a class-level guard |

### Hard rules

- **Never hides a bug.** No swallowed exceptions, ignore pragmas, special-cased test inputs, or weakened tests.
- **Stops when a test contradicts the intended behaviour.** It reports the conflict and asks which one is right.
- **Stays in scope.** Unrelated problems go under "Also noticed".
- **Asks before big moves.** Refactors, contract changes and new dependencies need your OK.
- **Never installs a package just because the code imports it.** AI-written code invents package names.

### What's in the skill

```
skills/exterminator/
  SKILL.md                  the workflow and rules
  scripts/
    find_variants.py        sibling search: ast-grep, then Semgrep, ripgrep, pure-Python fallback
    audit_fix.py            anti-cheat scan of the diff (edited assertions, empty catches, new deps...)
    bisect_repro.py         wrapper that makes `git bisect run` work with a repro command
    bug_log.py              read and append the project's bug-pattern log
  references/               bug classes, debugging, verifying variants, plus JS/TS, Python, Luau and C/C++ notes
  assets/                   template for the bug-pattern log
```

The scripts use only the Python 3 standard library. On Windows, call `python` instead of `python3`.

## Does it work? The lab

[`lab/`](lab) is a test rig that answers that with numbers. It runs headless Claude Code against a seeded multi-language shop backend (JS, Python, Luau, C), with and without the skill, and grades each run with checks the agent never sees:

- **11 tasks:** 8 bug tasks, each with one reported bug, hidden siblings and safe look-alikes, plus 3 behaviour traps (contradicting test, unreproducible report, one-line typo).
- **Scored on:** the reported bug fixed, sibling recall, look-alikes left alone, root cause named, and bad-fix flags (edited assertions, swallowed errors, forbidden dependencies).
- **Grader self-test:** `python3 lab/grading/selftest.py` proves the grader works with golden fixes and deliberate cheats. No model calls.

Setup, costs and how to read the scores are in [`lab/README.md`](lab/README.md).

## Layout

```
.claude-plugin/   plugin and marketplace manifests
skills/           the skill, ready to install
lab/              the eval rig
```

## License

[MIT](LICENSE)
