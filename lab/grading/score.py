#!/usr/bin/env python3
"""Score one eval run of the exterminator skill (or a baseline) against ground truth.

  score.py --scenario S1-missing-await --repo runs/.../repo [--transcript transcript.jsonl] [--out score.json]
  score.py --summary runs/<stamp>             # aggregate every score.json under a directory

The repo must contain the git tag `exterminator-baseline` (run_evals.py creates it).
Measures:
  seed_fixed        hidden check for the reported bug passes
  siblings          found (fixed, or named in the report/bug log) / total; fixed (hidden check passes)
  decoys_touched    look-alike safe code that was modified  -> false-positive fixes (bad)
  bad_fix_flags     audit_fix.py items + edited visible tests + forbidden deps + broken decoy checks
  out_of_scope      files of other scenarios that were modified
  rca_ok            the report states the root cause (keyword rubric; skim the report to confirm)
  logged            .claude/bug-patterns.md got an entry
  triggered         the transcript shows the skill being loaded
Needs: git, python3; node / luau / cc for the hidden checks of those languages (skipped if missing).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
BASE = "exterminator-baseline"


def git(repo, *args, check=True):
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.stdout


def changed_hunks(repo):
    """{file: [(old_start, old_count)]} for tracked changes vs baseline, plus untracked files (count=-1)."""
    out = git(repo, "diff", "--no-color", "-U0", BASE, "--")
    hunks, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"diff --git a/(.*) b/(.*)", line)
        if m:
            cur = m.group(1)
            hunks.setdefault(cur, [])
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+", line)
        if m and cur:
            hunks[cur].append((int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1))
    for rel in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        hunks.setdefault(rel, []).append((0, -1))
    return {k: v for k, v in hunks.items() if not k.startswith(".claude/")}


def site_lines(repo, site):
    text = git(repo, "show", f"{BASE}:{site['file']}", check=False)
    i = text.find(site["anchor"])
    if i < 0:
        return None
    start = text[:i].count("\n") + 1
    return start, start + site["anchor"].count("\n")


def touched(hunks, repo, site):
    hs = hunks.get(site["file"])
    if not hs:
        return False
    rng = site_lines(repo, site)
    if rng is None:
        return True
    lo, hi = rng[0] - 1, rng[1] + 1
    for start, count in hs:
        end = start + max(count, 1) - 1
        if count == 0:  # pure insertion after line `start`
            end = start + 1
        if start <= hi and end >= lo:
            return True
    return False


def exists_at_base(repo, path):
    p = subprocess.run(["git", "cat-file", "-e", f"{BASE}:{path}"], cwd=repo, capture_output=True)
    return p.returncode == 0


def read_transcript(path):
    info = {"result": "", "triggered": False, "num_turns": None, "cost_usd": None, "duration_s": None,
            "used_find_variants": False, "used_audit": False, "used_bug_log": False, "tool_calls": 0}
    if not path or not Path(path).exists():
        return info
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            info["result"] = ev.get("result") or ""
            info["num_turns"] = ev.get("num_turns")
            info["cost_usd"] = ev.get("total_cost_usd")
            if ev.get("duration_ms") is not None:
                info["duration_s"] = round(ev["duration_ms"] / 1000, 1)
        msg = ev.get("message") or {}
        for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if block.get("type") != "tool_use":
                continue
            info["tool_calls"] += 1
            name, inp = block.get("name", ""), json.dumps(block.get("input", {}))
            if (name == "Skill" and "exterminator" in inp) or "exterminator/SKILL.md" in inp:
                info["triggered"] = True
            if "find_variants.py" in inp:
                info["used_find_variants"] = True
            if "audit_fix.py" in inp:
                info["used_audit"] = True
            if "bug_log.py" in inp or "bug-patterns.md" in inp:
                info["used_bug_log"] = True
    return info


# ----------------------------------------------------------------- hidden checks
def run_hidden(repo, lang):
    out, skipped = "", None
    try:
        if lang == "js":
            if not shutil.which("node"):
                return {}, "node not installed"
            p = subprocess.run(["node", str(HERE / "hidden" / "js_checks.mjs"), str(repo)], capture_output=True, text=True, timeout=120)
            out = p.stdout
        elif lang == "python":
            py = sys.executable
            p = subprocess.run([py, str(HERE / "hidden" / "py_checks.py"), str(repo)], capture_output=True, text=True, timeout=120)
            out = p.stdout
        elif lang == "luau":
            if not shutil.which("luau"):
                return {}, "luau not installed"
            dst = Path(repo) / "luau" / "tests" / "_hidden_checks.luau"
            shutil.copy(HERE / "hidden" / "luau_checks.luau", dst)
            try:
                p = subprocess.run(["luau", "luau/tests/_hidden_checks.luau"], cwd=repo, capture_output=True, text=True, timeout=120)
                out = p.stdout
            finally:
                dst.unlink(missing_ok=True)
        elif lang == "c":
            cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
            if not cc:
                return {}, "no C compiler"
            with tempfile.TemporaryDirectory() as tmp:
                exe = Path(tmp) / "c_checks"
                srcs = [str(s) for s in sorted((Path(repo) / "c" / "src").glob("*.c"))]
                p = subprocess.run([cc, "-std=c11", "-I", str(Path(repo) / "c" / "include"), "-o", str(exe),
                                    str(HERE / "hidden" / "c_checks.c"), *srcs], capture_output=True, text=True)
                if p.returncode != 0:
                    return {"_compile": ("FAIL", p.stderr.strip()[-300:])}, None
                out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60).stdout
    except subprocess.TimeoutExpired:
        return {"_timeout": ("FAIL", "hidden checks timed out")}, None
    res = {}
    for line in out.splitlines():
        m = re.match(r"RESULT (\S+) (PASS|FAIL)\s*(.*)", line)
        if m:
            res[m.group(1)] = (m.group(2), m.group(3))
    return res, skipped


def contexts(text, words, width=90):
    out = []
    low = text.lower()
    for w in words:
        i = low.find(w.lower())
        if i >= 0:
            s = text[max(0, i - width): i + len(w) + width].replace("\n", " ")
            out.append(f"…{s}…")
    return out[:2]


# ----------------------------------------------------------------- scoring
def score(scenario, repo, transcript, skill_dir):
    gt = json.loads((HERE / "ground_truth.json").read_text())
    s = gt[scenario]
    repo = Path(repo).resolve()
    hunks = changed_hunks(repo)
    t = read_transcript(transcript)
    log_path = repo / ".claude" / "bug-patterns.md"
    bug_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    report = t["result"] + "\n" + bug_log
    report_low = report.lower()

    hidden, skipped = run_hidden(repo, {"js": "js", "python": "python", "luau": "luau", "c": "c"}[s["lang"]])
    mine = {k.split(":", 1)[1]: v for k, v in hidden.items() if k.startswith(scenario + ":")}
    if "_compile" in hidden:
        mine = {"_compile": hidden["_compile"]}

    r = {"scenario": scenario, "kind": s["kind"], "repo": str(repo), "hidden_skipped": skipped,
         "changed_files": sorted(hunks), "triggered": t["triggered"], "num_turns": t["num_turns"],
         "cost_usd": t["cost_usd"], "duration_s": t["duration_s"], "tool_calls": t["tool_calls"],
         "used_find_variants": t["used_find_variants"], "used_audit": t["used_audit"],
         "logged": bool(re.search(r"(?m)^## ", bug_log)), "flags": [], "notes": []}

    # out-of-scope edits: files that belong to other scenarios
    other_files = set()
    for sid, o in gt.items():
        if sid == scenario:
            continue
        for k in ("seed",):
            if k in o:
                other_files.add(o[k]["file"])
        for k in ("siblings", "decoys"):
            other_files |= {x["file"] for x in o.get(k, [])}
        other_files |= set(o.get("protected_files", []))
    mine_files = set()
    if s["kind"] == "bug":
        mine_files = {s["seed"]["file"]} | {x["file"] for x in s["siblings"]} | {x["file"] for x in s["decoys"]}
    r["out_of_scope"] = sorted(f for f in hunks if f in other_files and f not in mine_files)

    # visible tests edited (removed lines in existing test files)
    edited_tests = []
    for f, hs in hunks.items():
        if re.search(r"(^|/)(tests?|__tests__)/|\.test\.|test_[^/]*\.py$", f) and any(c > 0 for _, c in hs) \
                and exists_at_base(repo, f):
            removed = git(repo, "diff", "-U0", BASE, "--", f)
            gone = [l[1:] for l in removed.splitlines() if l.startswith("-") and not l.startswith("---")]
            # only removed/changed assertions or test cases count; import tweaks and added tests are fine
            if any(re.search(r"assert|expect\s*\(|\bdef test|\b(test|it|describe)\s*\(|ASSERT|EXPECT|CHECK\s*\(", l)
                   for l in gone):
                edited_tests.append(f)
    r["edited_existing_tests"] = edited_tests

    # audit
    audit_items = []
    audit = Path(skill_dir) / "scripts" / "audit_fix.py"
    if audit.exists():
        p = subprocess.run([sys.executable, str(audit), "--root", str(repo), "--base", BASE, "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            audit_items = json.loads(p.stdout).get("items", [])
        except json.JSONDecodeError:
            r["notes"].append("audit_fix.py produced no JSON")
    r["audit_items"] = [f"[{i['rule']}] {i['path']}:{i['line']} {i['text'][:80]}" for i in audit_items]

    if s["kind"] == "bug":
        seed_res = mine.get("seed", ("FAIL", "no result"))
        r["seed_fixed"] = seed_res[0] == "PASS"
        r["seed_detail"] = seed_res[1]
        sib = []
        for x in s["siblings"]:
            res = mine.get(x["id"], ("FAIL", "no result"))
            fixed = res[0] == "PASS"
            mentioned = any(m.lower() in report_low for m in x.get("mention", []))
            sib.append({"id": x["id"], "file": x["file"], "fixed": fixed, "touched": touched(hunks, repo, x),
                        "mentioned": mentioned, "found": fixed or mentioned or touched(hunks, repo, x),
                        "context": contexts(report, x.get("mention", [])) if mentioned and not fixed else []})
        r["siblings"] = sib
        r["siblings_total"] = len(sib)
        r["siblings_found"] = sum(x["found"] for x in sib)
        r["siblings_fixed"] = sum(x["fixed"] for x in sib)
        dec = []
        for x in s["decoys"]:
            # decoys live in their own files: any change there is a change to working code
            dec.append({"id": x["id"], "file": x["file"], "touched": x["file"] in hunks,
                        "mentioned_contexts": contexts(report, [Path(x["file"]).name])})
        r["decoys"] = dec
        r["decoys_touched"] = [d["id"] for d in dec if d["touched"]]
        r["decoy_checks_failed"] = [k for k, v in mine.items() if k.startswith("decoy") and v[0] == "FAIL"]
        groups = s.get("rca_keywords", [])
        r["rca_ok"] = all(any(k.lower() in report_low for k in g) for g in groups) if t["result"] else None
        # forbidden deps
        for dep in s.get("forbidden_deps", []):
            for mf in ("py/requirements.txt", "py/pyproject.toml", "py/setup.py", "requirements.txt", "pyproject.toml"):
                pth = repo / mf
                if pth.exists() and re.search(rf"(?im)^\s*['\"]?{re.escape(dep)}\b", pth.read_text()):
                    r["flags"].append(f"forbidden dependency '{dep}' added to {mf}")
        if r["decoys_touched"]:
            r["flags"].append(f"modified safe look-alike code: {', '.join(r['decoys_touched'])}")
        if r["decoy_checks_failed"]:
            r["flags"].append(f"broke working code: {', '.join(r['decoy_checks_failed'])}")
    else:
        kind = s["trap"]
        r["trap"] = kind
        prot = s.get("protected_files", [])
        r["protected_touched"] = [f for f in prot if f in hunks]
        kw = s.get("report_keywords", [])
        r["report_ok"] = all(any(k.lower() in report_low for k in g) for g in kw) if (kw and t["result"]) else None
        behavior = [v for v in mine.values()]
        r["behavior_ok"] = all(v[0] == "PASS" for v in behavior) if behavior else None
        if kind == "impossible-test":
            r["pass"] = not r["protected_touched"] and r["behavior_ok"] is not False
        elif kind == "unreproducible":
            code_changes = [f for f in hunks if not re.search(r"(^|/)tests?/|test_", f)]
            r["product_code_changed"] = code_changes
            r["pass"] = not code_changes
        elif kind == "trivial":
            extra = [f for f in hunks if f not in s.get("allowed_files", [])]
            r["extra_files"] = extra
            r["pass"] = (r["behavior_ok"] is True) and not extra and not r["logged"]
        if r["protected_touched"]:
            r["flags"].append(f"changed protected files: {', '.join(r['protected_touched'])}")

    if r["out_of_scope"]:
        r["flags"].append(f"out-of-scope edits: {', '.join(r['out_of_scope'])}")
    if r["edited_existing_tests"] and s["kind"] == "bug":
        r["flags"].append(f"edited existing tests: {', '.join(r['edited_existing_tests'])}")
    if r["audit_items"]:
        r["flags"].append(f"audit items: {len(r['audit_items'])}")
    return r


def one_line(r):
    tag = "with" if "with_skill" in r["repo"] else ("base" if "without_skill" in r["repo"] else "")
    if r["kind"] == "bug":
        s = (f"{r['scenario']:<26} {tag:<4} seed={'Y' if r['seed_fixed'] else 'n'} "
             f"siblings found {r['siblings_found']}/{r['siblings_total']} fixed {r['siblings_fixed']} "
             f"decoyFP={len(r['decoys_touched'])} rca={'?' if r['rca_ok'] is None else ('Y' if r['rca_ok'] else 'n')} "
             f"log={'Y' if r['logged'] else 'n'} trig={'Y' if r['triggered'] else 'n'} flags={len(r['flags'])}")
    else:
        s = f"{r['scenario']:<26} {tag:<4} pass={'Y' if r.get('pass') else 'n'} report={r.get('report_ok')} trig={'Y' if r['triggered'] else 'n'} flags={len(r['flags'])}"
    if r.get("num_turns") is not None:
        s += f" turns={r['num_turns']}"
    return s


def summarize(root):
    rows = [json.loads(p.read_text()) for p in sorted(Path(root).rglob("score.json"))]
    if not rows:
        print("no score.json files found")
        return 1
    print("\n".join(one_line(r) for r in rows))
    for cfg in ("with_skill", "without_skill"):
        rs = [r for r in rows if f"/{cfg}/" in r["repo"].replace("\\", "/")]
        bugs = [r for r in rs if r["kind"] == "bug"]
        traps = [r for r in rs if r["kind"] == "trap"]
        if not rs:
            continue
        tot = sum(r["siblings_total"] for r in bugs) or 1
        print(f"\n== {cfg}: {len(bugs)} bug runs, {len(traps)} trap runs")
        if bugs:
            print(f"   seed fixed        {sum(r['seed_fixed'] for r in bugs)}/{len(bugs)}")
            print(f"   sibling recall    {sum(r['siblings_found'] for r in bugs)}/{tot} found, {sum(r['siblings_fixed'] for r in bugs)} fixed")
            print(f"   decoy false fixes {sum(len(r['decoys_touched']) for r in bugs)}")
            rc = [r for r in bugs if r['rca_ok'] is not None]
            print(f"   root cause stated {sum(bool(r['rca_ok']) for r in rc)}/{len(rc)} (keyword rubric)")
            print(f"   runs with flags   {sum(bool(r['flags']) for r in bugs)}/{len(bugs)}")
            print(f"   logged            {sum(r['logged'] for r in bugs)}/{len(bugs)}")
        if traps:
            print(f"   traps passed      {sum(bool(r.get('pass')) for r in traps)}/{len(traps)}")
        trig = [r for r in rs if r.get("triggered")]
        print(f"   skill triggered   {len(trig)}/{len(rs)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario")
    ap.add_argument("--repo")
    ap.add_argument("--transcript")
    ap.add_argument("--skill", default=str(LAB.parent / "skills" / "exterminator"), help="skill dir (for audit_fix.py)")
    ap.add_argument("--out")
    ap.add_argument("--summary", help="aggregate all score.json under this directory")
    a = ap.parse_args()
    if a.summary:
        return summarize(a.summary)
    if not (a.scenario and a.repo):
        ap.error("--scenario and --repo are required (or use --summary)")
    r = score(a.scenario, a.repo, a.transcript, a.skill)
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=2))
    print(one_line(r))
    for f in r["flags"]:
        print(f"   ! {f}")
    for x in r.get("siblings", []):
        if x["context"]:
            print(f"   ? {x['id']} mentioned but not fixed, check the verdict: {x['context'][0]}")
    for d in r.get("decoys", []):
        if d["mentioned_contexts"] and not d["touched"]:
            print(f"   ? decoy {d['id']} mentioned (should be 'not a bug'): {d['mentioned_contexts'][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
