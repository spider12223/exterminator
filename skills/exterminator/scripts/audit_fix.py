#!/usr/bin/env python3
"""Anti-cheat self-audit for a bug fix.

Scans the diff between --base (default: HEAD) and the working tree, including
untracked files, for the ways a "fix" can hide a bug instead of fixing it:

  test tampering   deleted test files, removed/edited assertions, new skip/only markers
  suppression      empty catch, bare except/pass, ignored pcall, ignore pragmas, `|| true`
  special-casing   test-environment branches, literals copied from test expectations
  config weakening strict/lint/type settings loosened, warnings disabled, CI failures allowed
  new dependencies packages added to manifests (verify they exist and are intended)
  leftovers        debug lines tagged EXTERMINATOR-DEBUG still in the code

Optionally runs the original repro command (--repro) and requires it to pass.

Exit codes: 0 PASS, 1 REVIEW (items to revert or justify) or FAIL (repro still fails), 2 error.
Every REVIEW item must be reverted or explained in the report. Some are
legitimate (e.g. a test that was genuinely wrong, and the user agreed).
Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testing)(/|$)"
    r"|(^|/)test_[^/]*\.py$|_test\.(py|go|c|cc|cpp|rs)$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$|\.(test|spec)\.luau?$|(^|/)conftest\.py$",
    re.I,
)
# Paths come from git, so always use "/". .claude/bugcam/ holds bugcam captures (recorded output,
# never code); its .claude is matched in any case because bugcam's mkdir lands in an existing .Claude/.
IGNORE_PATH = re.compile(
    r"(^|/)(node_modules|\.git|dist|build|\.next|coverage|__pycache__|\.venv|venv)/"
    r"|(^|/)(?i:\.claude)/bugcam/|\.claude/bug-patterns\.md$"
    r"|\.lock$|-lock\.json$"
)

ASSERTION = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bt\.(is|equal|deepEqual|true|false|throws)|"
    r"\bASSERT_\w+|\bEXPECT_\w+|\bCHECK\w*\s*\(|\bREQUIRE\w*\s*\(|\bself\.fail\b|\bpytest\.raises\b|"
    r"\bassertThrows\b|\.to(Be|Equal|Throw|Match|Have)\w*\(",
)
SKIP_MARKERS = re.compile(
    r"@pytest\.mark\.(skip|xfail)|@unittest\.(skip|expectedFailure)|\bpytest\.(skip|xfail)\s*\(|"
    r"\b(it|test|describe|context)\.(skip|only|todo)\s*\(|\b(xit|xdescribe|xtest|fit|fdescribe)\s*\(|"
    r"\{\s*(skip|todo)\s*:\s*true|\bskip\s*=\s*True\b|\bself\.skipTest\s*\(|GTEST_SKIP|DISABLED_\w+",
)

# (rule id, regex, scope, message). scope: "code" (non-test), "test", "any".
LINE_RULES = [
    ("empty-catch-js", r"catch\s*(\([^)]*\))?\s*\{\s*\}", "any", "empty catch block swallows the error"),
    ("promise-catch-noop", r"\.catch\s*\(\s*(\(\s*\w*\s*\)|\w+)\s*=>\s*(\{\s*\}|null|undefined|void 0|\[\]|\{\s*\}\s*\))", "any", ".catch that discards the error"),
    ("ts-suppress", r"@ts-(ignore|nocheck|expect-error)", "any", "TypeScript error suppressed"),
    ("eslint-disable", r"eslint-disable", "any", "lint rule disabled"),
    ("any-cast", r"\bas\s+any\b|<any>|:\s*any\b|::\s*any\b", "code", "cast to any hides a type error"),
    ("py-bare-except", r"^\s*except\s*:", "any", "bare except catches everything"),
    ("py-type-ignore", r"#\s*type:\s*ignore|#\s*pyright:\s*ignore|#\s*noqa\b|#\s*pragma:\s*no cover", "any", "type/lint check suppressed"),
    ("contextlib-suppress", r"contextlib\.suppress\s*\(|\bwith\s+suppress\s*\(", "code", "exceptions suppressed"),
    ("luau-mode-downgrade", r"^\s*--!\s*(nonstrict|nocheck)\b", "any", "Luau type checking downgraded"),
    ("luau-pcall-ignored", r"^\s*pcall\s*\(", "code", "pcall result ignored: errors silently dropped"),
    ("luau-selene-allow", r"--\s*selene:\s*allow", "any", "Selene lint suppressed"),
    ("cpp-catch-all", r"catch\s*\(\s*\.\.\.\s*\)\s*\{\s*\}", "any", "catch(...) swallows everything"),
    ("c-pragma-ignore", r"#\s*pragma\s+(GCC|clang)\s+diagnostic\s+ignored|\bNOLINT\b", "any", "compiler/lint warning suppressed"),
    ("shell-or-true", r"\|\|\s*(true|:)\b|\bset\s+\+e\b", "any", "command failure ignored"),
    ("git-no-verify", r"--no-verify\b", "any", "hooks bypassed"),
    ("test-env-branch",
     r"NODE_ENV\s*[!=]==?\s*['\"]test|['\"]test['\"]\s*[!=]==?\s*process\.env|JEST_WORKER_ID|\bVITEST\b|PYTEST_CURRENT_TEST|"
     r"['\"]pytest['\"]\s+in\s+sys\.modules|\bIS_TEST\b|\b__TEST__\b|\bisTestEnv\b",
     "code", "code behaves differently under test: special-casing"),
    ("debug-leftover", r"EXTERMINATOR-DEBUG", "any", "temporary debug logging left in"),
]
MULTILINE_RULES = [
    ("py-except-pass", r"^\s*except\b[^\n]*:\s*\n\s*(pass|\.\.\.|return(\s+None)?|continue)\s*$", "any", "exception caught and dropped"),
    ("js-empty-catch-ml", r"catch\s*(\([^)]*\))?\s*\{\s*\n\s*\}", "any", "empty catch block swallows the error"),
    ("luau-pcall-unchecked", r"local\s+_?\w*\s*,?\s*_?\w*\s*=\s*pcall\([^\n]*\)\s*\n(?![^\n]*\bif\b)", "code", "pcall result captured but not checked on the next line"),
]
CONFIG_RULES = [
    (r"(^|/)tsconfig[^/]*\.json$", r"\"(strict|strictNullChecks|noImplicitAny|noUncheckedIndexedAccess)\"\s*:\s*false", "TypeScript strictness loosened"),
    (r"(^|/)(\.eslintrc[^/]*|eslint\.config\.[cm]?[jt]s|package\.json)$", r"['\"]?(off|warn)['\"]?\s*[,\]]|:\s*0\s*[,}]", "lint rule turned off or downgraded"),
    (r"(^|/)(mypy\.ini|setup\.cfg|pyproject\.toml|pyrightconfig\.json)$", r"ignore_errors\s*=\s*(true|True)|\"?typeCheckingMode\"?\s*[:=]\s*\"?(off|basic)|strict\s*=\s*false", "Python type checking loosened"),
    (r"(^|/)(ruff\.toml|pyproject\.toml|\.flake8|setup\.cfg)$", r"\b(ignore|extend-ignore|per-file-ignores)\b", "lint rules ignored"),
    (r"(^|/)\.luaurc$", r"\"languageMode\"\s*:\s*\"(nonstrict|nocheck)\"", "Luau type checking loosened"),
    (r"(^|/)selene\.toml$", r"=\s*\"allow\"", "Selene lint allowed"),
    (r"(^|/)(Makefile|CMakeLists\.txt|[^/]*\.mk|meson\.build)$", r"-Wno-|\s-w(\s|$)|-fpermissive", "compiler warnings disabled"),
    (r"(^|/)\.github/workflows/|(^|/)\.gitlab-ci\.yml$", r"continue-on-error:\s*true|allow_failure:\s*true", "CI failure allowed"),
    (r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|jest\.config\.[cm]?[jt]s|vitest\.config\.[cm]?[jt]s)$", r"--deselect|-k\s+['\"]?not\b|testPathIgnorePatterns|exclude\s*:", "tests excluded from the run"),
]
STR_LIT = re.compile(r"(['\"])((?:(?!\1).){4,80})\1")
NUM_LIT = re.compile(r"(?<![\w.])(-?\d+\.\d+|-?\d{3,})(?![\w.])")


@dataclass
class FileDiff:
    path: str
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)
    status: str = "M"  # M, A, D, ?
    old_path: str = ""  # rename source, if git paired this file with one


@dataclass
class Item:
    rule: str
    path: str
    line: int
    text: str
    msg: str


def git(args, cwd, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"git {' '.join(args)} failed")
    return p.stdout


def parse_diff(text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    cur = None
    old_ln = new_ln = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)", line)
            cur = FileDiff(m.group(2)) if m else None
            if cur:
                files[cur.path] = cur
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur.status = "A"
        elif line.startswith("deleted file mode"):
            cur.status = "D"
        elif line.startswith("rename from "):
            cur.old_path = line[len("rename from "):]
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            cur.added.append((new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-"):
            cur.removed.append((old_ln, line[1:]))
            old_ln += 1
        elif line.startswith(" "):
            old_ln += 1
            new_ln += 1
    return files


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path.replace("\\", "/")))


def scope_ok(scope: str, path: str) -> bool:
    return scope == "any" or (scope == "test") == is_test(path)


def consecutive_blocks(lines):
    block, prev = [], None
    for ln, txt in lines:
        if prev is not None and ln != prev + 1:
            yield block
            block = []
        block.append((ln, txt))
        prev = ln
    if block:
        yield block


def expected_literals(root: Path) -> set[str]:
    """String/number literals that appear on assertion lines in the repo's tests."""
    lits: set[str] = set()
    try:
        tracked = git(["ls-files"], root).splitlines() + git(["ls-files", "--others", "--exclude-standard"], root).splitlines()
    except RuntimeError:
        return lits
    for rel in tracked:
        if not is_test(rel) or IGNORE_PATH.search(rel):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not ASSERTION.search(line):
                continue
            for m in STR_LIT.finditer(line):
                s = m.group(2)
                if re.search(r"[A-Za-z0-9]", s) and not re.fullmatch(r"[\w./-]*\.(js|ts|py|luau?|c|h)", s):
                    lits.add(m.group(0)[1:-1])
            for m in NUM_LIT.finditer(line):
                if m.group(1) not in {"100", "1000", "200", "404", "500"}:
                    lits.add(m.group(1))
    return lits


def manifest_deps(text: str, path: str) -> set[str]:
    deps: set[str] = set()
    name = Path(path).name
    if name == "package.json":
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            return deps
        for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps |= set((d.get(k) or {}).keys())
    elif re.match(r"requirements.*\.txt$", name) or name.endswith(".in"):
        for ln in text.splitlines():
            ln = ln.split("#")[0].strip()
            m = re.match(r"([A-Za-z0-9_.\-]+)", ln)
            if m and not ln.startswith("-"):
                deps.add(m.group(1).lower())
    elif name in ("pyproject.toml", "wally.toml", "Cargo.toml", "Pipfile"):
        try:
            import tomllib  # py3.11+
            d = tomllib.loads(text)
        except Exception:
            return {ln.strip() for ln in text.splitlines() if "=" in ln or ln.strip().startswith('"')}
        proj = d.get("project", {})
        for spec in proj.get("dependencies", []) or []:
            m = re.match(r"([A-Za-z0-9_.\-]+)", spec)
            if m:
                deps.add(m.group(1).lower())
        for group in (proj.get("optional-dependencies") or {}).values():
            for spec in group:
                m = re.match(r"([A-Za-z0-9_.\-]+)", spec)
                if m:
                    deps.add(m.group(1).lower())
        for sect in ("dependencies", "dev-dependencies", "server-dependencies", "packages", "dev-packages"):
            deps |= set((d.get(sect) or {}).keys())
        poetry = d.get("tool", {}).get("poetry", {})
        deps |= set((poetry.get("dependencies") or {}).keys())
    return deps


MANIFEST = re.compile(r"(^|/)(package\.json|requirements[^/]*\.txt|[^/]*requirements[^/]*\.in|pyproject\.toml|wally\.toml|Cargo\.toml|Pipfile)$")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="path inside the git repo (default: .)")
    ap.add_argument("--base", default="HEAD", help="git ref to diff against (default: HEAD)")
    ap.add_argument("--repro", help="shell command that reproduced the bug; must now exit 0")
    ap.add_argument("--timeout", type=int, default=900, help="seconds allowed for --repro (default 900)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        root = Path(git(["rev-parse", "--show-toplevel"], a.root).strip())
    except (RuntimeError, FileNotFoundError) as e:
        print(f"AUDIT: ERROR - not a git repository ({e}). Without version control this audit can't see your changes;"
              " review your own diff for the patterns in hard rule 1, or `git init && git add -A && git commit` before fixing next time.")
        return 2

    try:
        diff_text = git(["diff", "--no-color", "--no-ext-diff", "-U0", "-M", a.base, "--"], root)
    except RuntimeError as e:
        print(f"AUDIT: ERROR - {e}")
        return 2
    files = parse_diff(diff_text)
    for rel in git(["ls-files", "--others", "--exclude-standard"], root).splitlines():
        if IGNORE_PATH.search(rel):
            continue
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        fd = FileDiff(rel, status="?")
        fd.added = list(enumerate(lines, 1))
        files[rel] = fd

    items: list[Item] = []
    lits = expected_literals(root)

    for path, fd in files.items():
        # a test renamed to an ignored or non-test path has left the suite: same as deleting it
        if (fd.old_path and is_test(fd.old_path) and not IGNORE_PATH.search(fd.old_path)
                and (IGNORE_PATH.search(path) or not is_test(path))):
            items.append(Item("test-deleted", fd.old_path, 0, f"-> {path}", "test file moved out of the test suite"))
        if IGNORE_PATH.search(path):
            continue
        test = is_test(path)

        # test tampering
        if test and fd.status == "D":
            items.append(Item("test-deleted", path, 0, "", "test file deleted"))
        if test and fd.status != "D":
            for ln, txt in fd.removed:
                if ASSERTION.search(txt):
                    items.append(Item("assertion-removed", path, ln, txt.strip(), "assertion removed or edited (base line number)"))
            for ln, txt in fd.added:
                if SKIP_MARKERS.search(txt):
                    items.append(Item("test-skipped", path, ln, txt.strip(), "test skipped/disabled or narrowed with .only"))
        if fd.status == "D":
            continue

        # line rules
        for rid, rx, scope, msg in LINE_RULES:
            if not scope_ok(scope, path):
                continue
            r = re.compile(rx)
            for ln, txt in fd.added:
                if r.search(txt):
                    items.append(Item(rid, path, ln, txt.strip(), msg))
        for rid, rx, scope, msg in MULTILINE_RULES:
            if not scope_ok(scope, path):
                continue
            r = re.compile(rx, re.M)
            for block in consecutive_blocks(fd.added):
                joined = "\n".join(t for _, t in block)
                for m in r.finditer(joined):
                    idx = joined[: m.start()].count("\n")
                    ln, txt = block[idx]
                    items.append(Item(rid, path, ln, txt.strip(), msg))

        # literals copied from test expectations into non-test code
        if not test and lits:
            for ln, txt in fd.added:
                if not re.search(r"\breturn\b|[!=]==?|\bif\b|\bcase\b|\bwhen\b|=>", txt):
                    continue
                found = [l for l in lits if (f'"{l}"' in txt or f"'{l}'" in txt or re.search(rf"(?<![\w.]){re.escape(l)}(?![\w.])", txt))]
                if found:
                    items.append(Item("literal-from-test", path, ln, txt.strip(),
                                      f"uses a value that a test asserts on ({', '.join(found[:3])}): make sure the answer isn't hardcoded"))

        # config weakening
        for prx, rx, msg in CONFIG_RULES:
            if re.search(prx, path):
                r = re.compile(rx)
                for ln, txt in fd.added:
                    if r.search(txt):
                        items.append(Item("config-weakened", path, ln, txt.strip(), msg))

        # new dependencies
        if MANIFEST.search(path):
            try:
                old = git(["show", f"{a.base}:{path}"], root, check=False) if fd.status not in ("A", "?") else ""
                new = (root / path).read_text(encoding="utf-8", errors="replace")
                added = manifest_deps(new, path) - manifest_deps(old, path)
            except OSError:
                added = set()
            for dep in sorted(added):
                items.append(Item("new-dependency", path, 0, dep,
                                  "new dependency: confirm it exists in the registry, is the package the code meant, and the user approved it"))

    # repro
    repro_result = None
    if a.repro:
        try:
            p = subprocess.run(a.repro, shell=True, cwd=root, capture_output=True, text=True, timeout=a.timeout,
                               encoding="utf-8", errors="replace")
            repro_result = p.returncode
            if p.returncode != 0:
                tail = (p.stdout + p.stderr).strip().splitlines()[-5:]
                items.append(Item("repro-fails", "(repro)", 0, a.repro, "the original repro still fails:\n      " + "\n      ".join(tail)))
        except subprocess.TimeoutExpired:
            repro_result = "timeout"
            items.append(Item("repro-timeout", "(repro)", 0, a.repro, f"repro did not finish within {a.timeout}s"))

    changed = [p for p, f in files.items() if not IGNORE_PATH.search(p) or (f.old_path and not IGNORE_PATH.search(f.old_path))]
    repro_broken = any(i.rule in ("repro-fails", "repro-timeout") for i in items)
    status = "FAIL" if repro_broken else ("REVIEW" if items else "PASS")
    if a.json:
        print(json.dumps({"status": status, "changed_files": changed,
                          "repro_exit": repro_result, "items": [i.__dict__ for i in items]}, indent=2))
        return 0 if not items else 1

    print(f"Audited {len(changed)} changed file(s) against {a.base}"
          + (f"; repro exit code {repro_result}" if a.repro else "; no --repro given (run it yourself)"))
    if not changed:
        print("Note: no changes found. If you already committed the fix, pass --base <commit-before-fix>.")
    if not items:
        print("AUDIT: PASS")
        return 0
    if repro_broken:
        print(f"AUDIT: FAIL - the original repro still fails, so the bug is not fixed yet ({len(items)} item(s))\n")
    else:
        print(f"AUDIT: REVIEW ({len(items)} item(s)) - revert each, or justify it in the report\n")
    for i in items:
        loc = f"{i.path}:{i.line}" if i.line else i.path
        print(f"  [{i.rule}] {loc}\n      {i.msg}")
        if i.text and i.rule not in ("repro-fails",):
            print(f"      > {i.text[:160]}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into `head` etc.
        sys.exit(0)
