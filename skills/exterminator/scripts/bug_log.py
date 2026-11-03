#!/usr/bin/env python3
"""Read and append the project's bug-pattern log (.claude/bug-patterns.md).

The log is how future sessions learn from past bugs: each entry records the
bug class, where it started, the broken assumption, the exact search command
that found its siblings (so it can be rerun), what happened to each sibling,
and the guard that was added.

  bug_log.py show                       # all entries, newest last
  bug_log.py show --cls missing-await   # entries of one class
  bug_log.py show --grep saveOrder      # entries mentioning a word
  bug_log.py searches                   # just the stored search commands, to rerun
  bug_log.py add --cls missing-await --lang js \\
      --seed "src/checkout.js:14 - order read before save finished" \\
      --assumption "saveOrder() is synchronous" \\
      --search "python3 scripts/find_variants.py --lang js --symbol saveOrder" \\
      --sibling "src/refund.js:9 | fixed" \\
      --sibling "src/jobs/nightly.js:12 | needs OK | forEach(async)" \\
      --guard "no-floating-promises lint rule"

The log lives at <repo root>/.claude/bug-patterns.md (override with --log).
Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

HEADER = """# Bug patterns

Written by the exterminator skill. Each entry is a bug class found in this
project, the assumption that broke, the search that finds its siblings, and
the guard that now prevents it. Future sessions read this first and rerun the
stored searches to catch new copies early. Keep entries short; edit freely.
"""
STATUSES = {"fixed", "needs ok", "not a bug", "suspected", "wontfix"}


def repo_root(start: Path) -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except FileNotFoundError:
        pass
    return start


def log_path(a) -> Path:
    if a.log:
        return Path(a.log)
    return repo_root(Path.cwd()) / ".claude" / "bug-patterns.md"


def entries(text: str) -> list[str]:
    parts = re.split(r"(?m)^(?=## )", text)
    return [p.strip() for p in parts if p.startswith("## ")]


def cmd_show(a) -> int:
    p = log_path(a)
    if not p.exists():
        print(f"No bug log yet ({p}). Nothing to check; you'll create it when you log this bug.")
        return 0
    es = entries(p.read_text(encoding="utf-8"))
    if a.cls:
        es = [e for e in es if re.search(rf"·\s*{re.escape(a.cls)}\s*(·|$)", e.splitlines()[0], re.I)]
    if a.grep:
        es = [e for e in es if a.grep.lower() in e.lower()]
    if not es:
        print("No matching entries.")
        return 0
    if a.last:
        es = es[-a.last:]
    classes = {}
    for e in es:
        head = e.splitlines()[0]
        bits = [b.strip() for b in head[3:].split("·")]
        if len(bits) >= 2:
            classes[bits[1]] = classes.get(bits[1], 0) + 1
    print(f"{len(es)} entr{'y' if len(es) == 1 else 'ies'} in {p}")
    if classes:
        print("Classes seen: " + ", ".join(f"{k} x{v}" for k, v in sorted(classes.items(), key=lambda kv: -kv[1])))
    print()
    print("\n\n".join(es))
    return 0


def cmd_searches(a) -> int:
    p = log_path(a)
    if not p.exists():
        print("No bug log yet.")
        return 0
    for e in entries(p.read_text(encoding="utf-8")):
        head = e.splitlines()[0][3:].strip()
        for line in e.splitlines():
            m = re.match(r"-\s*Search:\s*(.+)", line.strip())
            if m:
                print(f"# {head}\n{m.group(1).strip().strip('`')}\n")
    return 0


def norm_sibling(s: str) -> str:
    parts = [x.strip() for x in s.split("|")]
    loc = parts[0]
    status = parts[1].lower() if len(parts) > 1 else "suspected"
    if status in ("needs your ok", "ask", "pending"):
        status = "needs ok"
    note = " | ".join(parts[2:]) if len(parts) > 2 else ""
    if status not in STATUSES:
        print(f"warning: unusual sibling status '{status}' (expected one of {sorted(STATUSES)})", file=sys.stderr)
    return f"{loc} {status}" + (f" ({note})" if note else "")


def cmd_add(a) -> int:
    p = log_path(a)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(HEADER, encoding="utf-8")
    date = a.date or dt.date.today().isoformat()
    lines = [f"## {date} · {a.cls} · {a.lang}"]
    lines.append(f"- Seed: {a.seed}")
    lines.append(f"- Broken assumption: {a.assumption}")
    for s in a.search:
        lines.append(f"- Search: `{s}`")
    if a.sibling:
        lines.append("- Siblings: " + " · ".join(norm_sibling(s) for s in a.sibling))
    else:
        lines.append("- Siblings: none found")
    lines.append(f"- Guard: {a.guard or 'none yet'}")
    if a.lesson:
        lines.append(f"- Lesson: {a.lesson}")
    text = p.read_text(encoding="utf-8").rstrip() + "\n\n" + "\n".join(lines) + "\n"
    p.write_text(text, encoding="utf-8")
    print(f"Logged to {p}:\n")
    print("\n".join(lines))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", help="path to the log (default: <repo>/.claude/bug-patterns.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="print entries")
    s.add_argument("--cls", help="only this bug class")
    s.add_argument("--grep", help="only entries containing this text")
    s.add_argument("--last", type=int, help="only the last N entries")
    s.set_defaults(fn=cmd_show)

    q = sub.add_parser("searches", help="print stored search commands to rerun")
    q.set_defaults(fn=cmd_searches)

    d = sub.add_parser("add", help="append an entry")
    d.add_argument("--cls", required=True, help="bug class, kebab-case (e.g. missing-await)")
    d.add_argument("--lang", required=True, help="language/stack (js, ts, python, luau, c, cpp, ...)")
    d.add_argument("--seed", required=True, help="'path:line - what broke'")
    d.add_argument("--assumption", required=True, help="the assumption that turned out false")
    d.add_argument("--search", action="append", default=[], required=True, help="search command that finds siblings (repeatable)")
    d.add_argument("--sibling", action="append", default=[], help="'path:line | fixed|needs OK|not a bug|suspected | note' (repeatable)")
    d.add_argument("--guard", help="test/lint rule/type/assertion added to prevent the class")
    d.add_argument("--lesson", help="one-sentence lesson")
    d.add_argument("--date", help="override date (YYYY-MM-DD)")
    d.set_defaults(fn=cmd_add)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into `head` etc.
        sys.exit(0)
