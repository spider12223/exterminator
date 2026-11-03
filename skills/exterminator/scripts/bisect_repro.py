#!/usr/bin/env python3
"""Wrapper for `git bisect run` that maps outcomes to the exit codes bisect expects.

  0    good   (repro passes: the bug is NOT present at this commit)
  1    bad    (repro fails: the bug IS present)
  125  skip   (setup/build failed or repro timed out: this commit can't be tested)

Usage (run from the repo root; keep this script OUTSIDE the repo or copy it to
a temp dir first, because bisect checks out old commits):

  git bisect start <bad-commit> <good-commit>
  git bisect run python3 /path/to/bisect_repro.py \\
      --setup "npm ci --silent" \\
      --repro "node --test test/checkout.test.js"
  git bisect reset          # always, when done

--setup is optional (e.g. install deps or compile). If it fails, the commit is skipped.
--expect lets you require a specific failure text, so commits that fail for an
unrelated reason are skipped instead of being blamed:

  --repro "pytest tests/test_orders.py -x" --expect "KeyError: 'email'"

Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

GOOD, BAD, SKIP = 0, 1, 125


def sh(cmd: str, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repro", required=True, help="command that passes when the bug is absent")
    ap.add_argument("--setup", help="command to prepare the checkout (install/build); failure = skip")
    ap.add_argument("--expect", help="text that must appear in the failing output for the commit to count as bad")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()

    if a.setup:
        code, out = sh(a.setup, a.timeout)
        if code != 0:
            print(f"[bisect {head}] setup failed -> skip\n{out[-800:]}")
            return SKIP

    code, out = sh(a.repro, a.timeout)
    if code == -1:
        print(f"[bisect {head}] repro timed out -> skip")
        return SKIP
    if code == 0:
        print(f"[bisect {head}] repro passes -> good")
        return GOOD
    if a.expect and a.expect not in out:
        print(f"[bisect {head}] failed, but not with the expected error -> skip\n{out[-800:]}")
        return SKIP
    print(f"[bisect {head}] repro fails -> bad")
    return BAD


if __name__ == "__main__":
    sys.exit(main())
