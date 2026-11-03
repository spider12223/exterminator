<p align="center">
  <img src="assets/banner.svg" alt="exterminator: a stick-figure exterminator sprays a code bug while its headlamp reveals a whole nest of bugs hiding in the code" width="100%">
</p>

<p align="center">
  A skill for <b>Claude Code</b> and <b>Codex</b> that fixes bugs at the root cause,<br>then hunts down every other place the same bug lives.
</p>

<p align="center">
  <a href="#claude-code"><img alt="Claude Code" src="https://img.shields.io/badge/Claude_Code-skill-D97757?style=flat-square"></a>
  <a href="#codex"><img alt="Codex" src="https://img.shields.io/badge/Codex-skill-10A37F?style=flat-square"></a>
  <a href="lab"><img alt="Eval lab" src="https://img.shields.io/badge/evals-11_tasks-84CC16?style=flat-square"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-9CA3AF?style=flat-square"></a>
</p>

---

Most agents patch the line that threw, watch the test go green, and move on. But the real cause is usually a wrong assumption or a copy-pasted pattern, and it tends to live in several files at once. Next week it's back with a different stack trace.

`exterminator` reproduces the bug, traces the real cause, finds the siblings, fixes the source, locks it down with a regression test, and explains what happened in plain language.

| A typical fix | With exterminator |
|---|---|
| Patches the line in the stack trace | Traces symptom → proximate cause → root cause, with evidence |
| Stops when the test is green | Hunts the same bug in other files, including ones that don't look alike |
| Sometimes edits the test to pass | Refuses: never weakens tests or swallows errors |
| `pip install` whatever the import says | Checks the package is real before touching it |
| "Fixed!" | Explains the cause so you learn something |

## Install

### Claude Code

```bash
npx skills add spider12223/exterminator -g -a claude-code -y
```

Or as a plugin, inside Claude Code:

```
/plugin marketplace add spider12223/exterminator
/plugin install exterminator@exterminator
```

### Codex

```bash
npx skills add spider12223/exterminator -g -a codex -y
```

Or as a plugin:

```bash
codex plugin marketplace add spider12223/exterminator
codex plugin add exterminator@exterminator
```

<details>
<summary><b>Manual install, or let the agent do it</b></summary>

<br>

```bash
git clone --depth 1 https://github.com/spider12223/exterminator.git

# Claude Code
mkdir -p ~/.claude/skills && cp -r exterminator/skills/exterminator ~/.claude/skills/

# Codex
mkdir -p ~/.agents/skills && cp -r exterminator/skills/exterminator ~/.agents/skills/
```

On Windows PowerShell, use `Copy-Item -Recurse` with `$HOME\.claude\skills\` or `$HOME\.agents\skills\`.

Or paste this to your agent:

> Install the exterminator skill from https://github.com/spider12223/exterminator (the folder is `skills/exterminator`) into my user-level skills folder.

</details>

## Use it

Report a bug the way you normally would: paste the error, say a test is red, or say something stopped working. The skill triggers on its own. In Codex you can also call it directly with `$exterminator`.

## How it works

```
reproduce ─► cause chain ─► name the bug class ─► hunt siblings ─► verify each one
    ─► fix the source ─► regression test ─► anti-cheat audit ─► report ─► log the pattern
```

It picks the smallest tier that fits, and escalates the moment the cause turns out to be shared:

| Tier | When | What it does |
|---|---|---|
| **T0** trivial | typo, cosmetic, a value you told it to change | fixes it, one-line report |
| **T1** local | wrong logic confined to one function | reproduces, checks callers and copies, adds a test |
| **T2** systemic | shared helper, copied logic, async, config, security, data loss, hallucinated package | the full workflow plus a class-level guard |

### Hard rules

- **Never hides a bug.** No swallowed exceptions, ignore pragmas, special-cased test inputs, or weakened tests.
- **Stops when a test contradicts the intended behaviour.** It reports the conflict and asks which one is right.
- **Stays in scope.** Unrelated problems go under "Also noticed".
- **Asks before big moves.** Refactors, contract changes and new dependencies need your OK.
- **Never installs a package just because the code imports it.** AI-written code invents package names, and attackers register them.

### What's inside

```
skills/exterminator/
├── SKILL.md                 the workflow and rules
├── agents/openai.yaml       Codex display metadata
├── scripts/
│   ├── find_variants.py     sibling search: ast-grep → Semgrep → ripgrep → pure Python
│   ├── audit_fix.py         anti-cheat scan of the diff: edited assertions, empty catches, new deps
│   ├── bisect_repro.py      makes `git bisect run` work with any repro command
│   └── bug_log.py           reads and appends the project's bug-pattern log
├── references/              bug classes, debugging, verifying variants, JS/TS, Python, Luau, C/C++
└── assets/                  bug-pattern log template
```

The scripts use only the Python 3 standard library. On Windows, call `python` instead of `python3`. The bug log lives at `.claude/bug-patterns.md` in your project, whichever agent you use.

## Does it work?

[`lab/`](lab) answers that with numbers. It runs the agent headless against a seeded shop backend in JS, Python, Luau and C, with and without the skill, then grades each run with checks the agent never sees.

- **11 tasks.** 8 bug tasks, each with one reported bug, 2–3 hidden siblings and 1–2 safe look-alikes. Plus 3 behaviour traps: a test that contradicts the spec, a report that can't be reproduced, and a plain typo.
- **Scored on** whether the reported bug is fixed, sibling recall, look-alikes left alone, whether the root cause is named, and bad-fix flags.
- **The grader is tested too.** `python3 lab/grading/selftest.py` runs golden fixes and deliberate cheats through it, with no model calls.

Setup, costs and how to read the scores are in [`lab/README.md`](lab/README.md). The runner drives Claude Code.

## Repo layout

```
.claude-plugin/        Claude Code plugin + marketplace manifests
.codex-plugin/         Codex plugin manifest
.agents/plugins/       Codex marketplace manifest
skills/exterminator/   the skill
lab/                   the eval rig
assets/                banner, icon and social preview
```

## License

[MIT](LICENSE)
