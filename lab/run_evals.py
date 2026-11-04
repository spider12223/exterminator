#!/usr/bin/env python3
"""Run the exterminator eval suite with Claude Code in headless mode, then score every run.

  python3 run_evals.py                                  # all tasks, with the skill, 1 run each
  python3 run_evals.py --tasks S1,S3,T1                 # a subset (prefix match on task ids)
  python3 run_evals.py --baseline                       # also run each task WITHOUT the skill
  python3 run_evals.py --repeat 3 --tasks S5            # variance check
  python3 run_evals.py --dry-run                        # build the run folders, don't call claude
  python3 run_evals.py --summary runs/2026-11-03_1015   # re-print the summary of an earlier batch

Each run gets a fresh copy of testbed/ as its own git repo (tagged `exterminator-baseline`).
With-skill runs get the skill as a project skill in .claude/skills/exterminator/.
These are real model runs and use your Claude plan/API credits: start with a subset.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
TOOLS = "Read,Edit,Write,Glob,Grep,Bash,Skill,TodoWrite"


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(dst: Path, skill: Path | None):
    shutil.copytree(LAB / "testbed", dst, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "build"))
    if skill:
        shutil.copytree(skill, dst / ".claude" / "skills" / "exterminator",
                        ignore=shutil.ignore_patterns("evals", "__pycache__"))
    sh(["git", "init", "-q"], dst)
    sh(["git", "config", "user.email", "eval@example.com"], dst)
    sh(["git", "config", "user.name", "eval"], dst)
    sh(["git", "add", "-A"], dst)
    sh(["git", "commit", "-q", "-m", "baseline"], dst)
    sh(["git", "tag", "exterminator-baseline"], dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skill", default=str(LAB.parent / "skills" / "exterminator"), help="path to the exterminator skill folder")
    ap.add_argument("--tasks", help="comma-separated task id prefixes (default: all)")
    ap.add_argument("--baseline", action="store_true", help="also run without the skill")
    ap.add_argument("--no-skill-runs", action="store_true", help="only run the baseline")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", help="passed to claude --model")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per run")
    ap.add_argument("--claude", default="claude", help="claude CLI executable")
    ap.add_argument("--extra-args", default="", help="extra args for claude, e.g. '--permission-mode acceptEdits'")
    ap.add_argument("--out", default=str(LAB / "runs"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-personal-skill", action="store_true",
                    help="run even if ~/.claude/skills/exterminator exists (it would leak into baseline runs)")
    ap.add_argument("--summary", help="print the summary for an existing batch folder and exit")
    a = ap.parse_args()

    score_py = LAB / "grading" / "score.py"
    if a.summary:
        return subprocess.call([sys.executable, str(score_py), "--summary", a.summary])

    skill = Path(a.skill).resolve()
    if not (skill / "SKILL.md").exists():
        sys.exit(f"skill not found at {skill}")
    personal = Path.home() / ".claude" / "skills" / "exterminator"
    if personal.exists() and not a.allow_personal_skill and (a.baseline or a.no_skill_runs):
        sys.exit(f"{personal} exists, so 'without skill' runs would still see the skill. "
                 "Move it aside while running baselines, or pass --allow-personal-skill.")
    if not a.dry_run and not shutil.which(a.claude):
        sys.exit("claude CLI not found on PATH (install Claude Code, or pass --claude /path/to/claude)")

    tasks = json.loads((LAB / "tasks" / "tasks.json").read_text())
    if a.tasks:
        wanted = [t.strip() for t in a.tasks.split(",") if t.strip()]
        tasks = {k: v for k, v in tasks.items() if any(k.startswith(w) for w in wanted)}
    configs = [] if a.no_skill_runs else ["with_skill"]
    if a.baseline or a.no_skill_runs:
        configs.append("without_skill")
    gt = json.loads((LAB / "grading" / "ground_truth.json").read_text())
    web = [k for k in tasks if gt.get(k, {}).get("lang") == "web"]
    if web and importlib.util.find_spec("playwright") is None:
        # the scorer runs the web checks with this interpreter, so they'd all be skipped and scored as not fixed
        print(f"warning: {sys.executable} has no Playwright, so the hidden checks of {', '.join(web)} will be skipped.\n"
              f"  Install it: {sys.executable} -m pip install playwright && {sys.executable} -m playwright install chromium\n"
              f"  or leave those tasks out with --tasks.", file=sys.stderr)

    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    batch = Path(a.out) / stamp
    total = len(tasks) * len(configs) * a.repeat
    print(f"{total} run(s) -> {batch}")
    n = 0
    for tid, task in tasks.items():
        for cfg in configs:
            for rep in range(1, a.repeat + 1):
                n += 1
                run_dir = batch / tid / cfg / f"run-{rep}"
                repo = run_dir / "repo"
                run_dir.mkdir(parents=True, exist_ok=True)
                make_repo(repo, skill if cfg == "with_skill" else None)
                (run_dir / "prompt.txt").write_text(task["prompt"])
                transcript = run_dir / "transcript.jsonl"
                cmd = [a.claude, "-p", task["prompt"], "--output-format", "stream-json", "--verbose",
                       "--max-turns", str(a.max_turns), "--allowedTools", TOOLS]
                if a.model:
                    cmd += ["--model", a.model]
                if a.extra_args:
                    cmd += a.extra_args.split()
                print(f"[{n}/{total}] {tid} {cfg} run-{rep} ...", flush=True)
                if a.dry_run:
                    print("   " + " ".join(cmd[:2]) + " <prompt> " + " ".join(cmd[3:]))
                    continue
                with open(transcript, "w", encoding="utf-8") as fh:
                    try:
                        subprocess.run(cmd, cwd=repo, stdout=fh, stderr=subprocess.STDOUT, timeout=a.timeout)
                    except subprocess.TimeoutExpired:
                        print("   timed out")
                subprocess.run([sys.executable, str(score_py), "--scenario", tid, "--repo", str(repo),
                                "--transcript", str(transcript), "--skill", str(skill),
                                "--out", str(run_dir / "score.json")])
    if not a.dry_run:
        print()
        subprocess.call([sys.executable, str(score_py), "--summary", str(batch)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
