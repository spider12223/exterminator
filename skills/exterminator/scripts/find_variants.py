#!/usr/bin/env python3
r"""Find sibling instances of a bug pattern across a codebase.

Tries the most precise tool available and falls back automatically:
  ast-grep  ->  Semgrep  ->  ripgrep  ->  pure-Python regex scan

Every hit is tagged with how it was found:
  [ast]   structural match (ast-grep / Semgrep)
  [text]  regex match (lower precision: read the code before believing it)
  [def]   looks like a definition of --symbol
  [ref]   a reference/call of --symbol (import lines are hidden; --show-imports)

Nothing this script prints is a bug until you verify it
(see references/verifying-variants.md).

Examples:
  find_variants.py --lang js --symbol saveOrder
  find_variants.py --lang js --ast 'saveOrder($$$)' --regex 'saveOrder\('
  find_variants.py --lang python --regex 'except\s*:'
  find_variants.py --lang luau --ast '$A:FindFirstChild($$$)' --regex ':FindFirstChild\('
  find_variants.py --rule .claude/rules/missing-await.yml
A pattern that starts with '-' must be attached with '=':  --regex='->\s*Optional'
Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# language -> (ast-grep lang, semgrep lang or None, rg type or None, file globs)
LANGS = {
    "js": ("javascript", "javascript", "js", ["*.js", "*.mjs", "*.cjs", "*.jsx"]),
    "jsx": ("javascript", "javascript", "js", ["*.js", "*.jsx"]),
    "ts": ("typescript", "typescript", "ts", ["*.ts", "*.mts", "*.cts"]),
    "tsx": ("tsx", "typescript", "ts", ["*.tsx", "*.ts"]),
    "python": ("python", "python", "py", ["*.py"]),
    "py": ("python", "python", "py", ["*.py"]),
    "lua": ("lua", "lua", "lua", ["*.lua"]),
    # Luau: ast-grep can parse it with the Lua grammar (error-tolerant) when
    # *.luau is mapped to lua; Semgrep does not scan .luau files.
    "luau": ("lua", None, None, ["*.luau", "*.lua"]),
    "c": ("c", "c", "c", ["*.c", "*.h"]),
    "cpp": ("cpp", "cpp", "cpp", ["*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.h"]),
    "go": ("go", "go", "go", ["*.go"]),
    "rust": ("rust", "rust", "rust", ["*.rs"]),
    "java": ("java", "java", "java", ["*.java"]),
    "cs": ("csharp", "csharp", "csharp", ["*.cs"]),
}

DEFAULT_EXCLUDES = [
    "node_modules", ".git", "dist", "build", "out", ".next", ".nuxt", "coverage",
    "vendor", "third_party", "__pycache__", ".venv", "venv", "env", ".tox",
    "target", "Packages", ".pytest_cache", ".mypy_cache", "*.min.js", "*.map",
    ".claude",  # skills and the bug log live here, not product code
]

DEF_PATTERNS = [
    r"\bfunction\s+{n}\b", r"\b(const|let|var)\s+{n}\s*=", r"\b{n}\s*[:=]\s*(async\s*)?(function|\()",
    r"\bdef\s+{n}\b", r"\bclass\s+{n}\b", r"\blocal\s+function\s+{n}\b", r"\bfunction\s+[\w.:]*[.:]{n}\b",
    r"\b{n}\s*=\s*function\b", r"^\s*(export\s+)?(async\s+)?function\s+{n}\b",
    r"^\s*(?!return\b)[\w\*&:<>,]+[\s\*&]+{n}\s*\([^;()]*\)\s*\{{?\s*$",  # C/C++ definition line
]
IMPORT_LINE = re.compile(
    r"^\s*(import\b|from\s+[\w.]+\s+import\b|export\s+\{[^}]*\}\s+from\b|#\s*include\b|using\b)"
    r"|\brequire\s*\(|^\s*local\s+\w+\s*=\s*require\b"
)


def which_ast_grep() -> str | None:
    for name in ("ast-grep", "sg"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        # On Linux `sg` is often the shadow-utils "switch group" command, not ast-grep.
        if "ast-grep" in (out.stdout + out.stderr).lower():
            return exe
    return None


def excluded(rel: str, excludes: list[str]) -> bool:
    parts = Path(rel).parts
    for pat in excludes:
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
        if fnmatch.fnmatch(rel, pat):
            return True
    return False


def walk_files(root: Path, globs: list[str] | None, excludes: list[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if not excluded(os.path.join(rel_dir, d), excludes)]
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if excluded(rel, excludes):
                continue
            if globs and not any(fnmatch.fnmatch(f, g) for g in globs):
                continue
            yield root / rel, rel


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- structural
def ast_grep_search(exe, root, lang, pattern=None, rule=None, excludes=()):
    ag_lang, _, _, globs = LANGS[lang] if lang else (None, None, None, None)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "sgconfig.yml"
        rule_dir = Path(tmp) / "rules"
        rule_dir.mkdir()
        cfg_text = "ruleDirs: [rules]\n"
        if lang == "luau":
            cfg_text += "languageGlobs:\n  lua: ['*.luau']\n"
        cfg.write_text(cfg_text)
        if rule:
            cmd = [exe, "scan", "-c", str(cfg), "--rule", str(rule), "--json=stream", "."]
        else:
            cmd = [exe, "run", "-c", str(cfg), "-l", ag_lang, "-p", pattern, "--json=stream", "."]
        for ex in excludes:
            if not any(ch in ex for ch in "*?["):
                cmd[-1:-1] = ["--globs", f"!**/{ex}/**"]
            else:
                cmd[-1:-1] = ["--globs", f"!{ex}"]
        proc = run(cmd, root)
    hits = []
    for line in proc.stdout.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        start = m.get("range", {}).get("start", {}).get("line", 0) + 1
        text = (m.get("lines") or m.get("text") or "").splitlines()[0:1]
        hits.append({"file": m.get("file"), "line": start, "text": text[0].strip() if text else "", "tag": "ast"})
    if proc.returncode not in (0, 1) and not hits and proc.stderr.strip():
        print(f"  ! ast-grep error: {proc.stderr.strip().splitlines()[-1]}", file=sys.stderr)
    return hits


def to_semgrep_pattern(p: str) -> str:
    p = re.sub(r"\$\$\$[A-Z_]*", "...", p)
    return p


def semgrep_search(exe, root, lang, pattern=None, rule=None, excludes=()):
    cmd = [exe, "scan", "--json", "--quiet", "--metrics=off", "--disable-version-check"]
    if rule:
        cmd += ["--config", str(rule)]
    else:
        cmd += ["--lang", LANGS[lang][1], "-e", to_semgrep_pattern(pattern)]
    for ex in excludes:
        cmd += ["--exclude", ex]
    cmd.append(".")
    proc = run(cmd, root)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        print("  ! semgrep produced no JSON", file=sys.stderr)
        return []
    hits = []
    for r in data.get("results", []):
        lines = (r.get("extra", {}).get("lines") or "").splitlines()
        hits.append({"file": r["path"], "line": r["start"]["line"], "text": lines[0].strip() if lines else "", "tag": "ast"})
    return hits


# ---------------------------------------------------------------- textual
def rg_search(exe, root, lang, regex, excludes, fixed=False, ignore_case=False, hidden=False):
    cmd = [exe, "--json", "-n"]
    if hidden:
        cmd.append("--hidden")
    if fixed:
        cmd.append("-F")
    else:
        cmd.append("-P") if needs_pcre(regex) else None
    if ignore_case:
        cmd.append("-i")
    if lang:
        for g in LANGS[lang][3]:
            cmd += ["-g", g]
    for ex in excludes:
        cmd += ["-g", f"!{ex}"]
    cmd += ["-e", regex, "."]
    proc = run(cmd, root)
    if proc.returncode == 2 and "-P" in cmd:  # PCRE2 not compiled in: retry with default engine
        cmd.remove("-P")
        proc = run(cmd, root)
    hits = []
    for line in proc.stdout.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") != "match":
            continue
        d = m["data"]
        path = d["path"].get("text", "")
        hits.append({"file": os.path.normpath(path), "line": d["line_number"],
                     "text": d["lines"].get("text", "").strip(), "tag": "text"})
    return hits


def needs_pcre(regex: str) -> bool:
    return bool(re.search(r"\(\?[=!<]|\\[1-9]", regex))


def py_search(root, lang, regex, excludes, fixed=False, ignore_case=False, hidden=False):
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(re.escape(regex) if fixed else regex, flags)
    globs = LANGS[lang][3] if lang else None
    hits = []
    for path, rel in walk_files(root, globs, excludes):
        if not hidden and any(part.startswith(".") for part in Path(rel).parts):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append({"file": rel, "line": i, "text": line.strip(), "tag": "text"})
        except OSError:
            continue
    return hits


def text_search(root, lang, regex, excludes, fixed=False, ignore_case=False, hidden=False):
    rg = shutil.which("rg")
    if rg:
        return rg_search(rg, root, lang, regex, excludes, fixed, ignore_case, hidden), "ripgrep"
    return py_search(root, lang, regex, excludes, fixed, ignore_case, hidden), "python-regex"


def fill_text(root: Path, hits):
    """Use the real source line (Semgrep may redact match text, ast-grep returns multi-line nodes)."""
    cache: dict[str, list[str]] = {}
    for h in hits:
        if h["tag"] != "ast":
            continue
        if h["file"] not in cache:
            try:
                cache[h["file"]] = (root / h["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[h["file"]] = []
        lines = cache[h["file"]]
        if 0 < h["line"] <= len(lines):
            h["text"] = lines[h["line"] - 1].strip()


def classify_symbol(hits, name):
    pats = [re.compile(p.format(n=re.escape(name))) for p in DEF_PATTERNS]
    for h in hits:
        if IMPORT_LINE.search(h["text"]):
            h["tag"] = "imp"
        elif any(p.search(h["text"]) for p in pats):
            h["tag"] = "def"
        else:
            h["tag"] = "ref"
    return hits


# ---------------------------------------------------------------- doctor
CHECKERS = [
    ("search", ["rg", "semgrep"]),
    ("js/ts", ["node", "tsc", "eslint", "biome"]),
    ("python", ["mypy", "pyright", "ruff"]),
    ("luau", ["luau", "luau-analyze", "luau-lsp", "selene"]),
    ("c/c++", ["cc", "gcc", "clang", "clang-tidy", "cppcheck"]),
    ("vcs", ["git"]),
]


def doctor() -> int:
    """Print which search engines and checkers exist, so you pick searches and guards that will run."""
    ag = which_ast_grep()
    print(f"ast-grep      {'yes' if ag else 'no'}  (structural search; Luau via the Lua grammar)")
    for group, tools in CHECKERS:
        found = [t for t in tools if shutil.which(t)]
        missing = [t for t in tools if t not in found]
        print(f"{group:<13} found: {', '.join(found) or '-'}" + (f"   missing: {', '.join(missing)}" if missing else ""))
    order = ["ast-grep" if ag else None, "semgrep" if shutil.which("semgrep") else None,
             "ripgrep" if shutil.which("rg") else "python-regex"]
    print("engine order: " + " -> ".join(o for o in order if o))
    if not ag and not shutil.which("semgrep"):
        print("note: no structural engine; --ast falls back to text search. Prefer --symbol and precise --regex.")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="directory to search (default: .)")
    ap.add_argument("--lang", choices=sorted(LANGS), help="language of the code being searched")
    ap.add_argument("--ast", help="ast-grep pattern, e.g. 'saveOrder($$$)'. Converted for Semgrep if ast-grep is missing")
    ap.add_argument("--rule", help="ast-grep rule YAML (has 'rule:') or Semgrep config (has 'rules:')")
    ap.add_argument("--regex", action="append", default=[], help="regex for the text search (repeatable)")
    ap.add_argument("--symbol", action="append", default=[], help="find definitions and references of this name (repeatable)")
    ap.add_argument("-F", "--fixed-strings", action="store_true", help="treat --regex as a literal string")
    ap.add_argument("-i", "--ignore-case", action="store_true")
    ap.add_argument("--hidden", action="store_true", help="also search dotfiles (.env.example, .github/, ...); text search only")
    ap.add_argument("--exclude", action="append", default=[], help="extra dir/glob to skip (repeatable)")
    ap.add_argument("--include-tests", action="store_true", help="also search test files (skipped by default for --symbol)")
    ap.add_argument("--show-imports", action="store_true", help="list import lines in --symbol results too")
    ap.add_argument("--max", type=int, default=200, help="cap printed hits (default 200)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--doctor", action="store_true", help="show which search engines and checkers are installed")
    a = ap.parse_args()

    if a.doctor:
        return doctor()
    if not (a.ast or a.rule or a.regex or a.symbol):
        ap.error("give at least one of --ast, --rule, --regex, --symbol (or --doctor)")
    if a.ast and not a.lang:
        ap.error("--ast needs --lang")

    root = Path(a.root).resolve()
    excludes = DEFAULT_EXCLUDES + a.exclude
    results: list[dict] = []
    notes: list[str] = []

    # structural search
    if a.ast or a.rule:
        ag = which_ast_grep()
        sem = shutil.which("semgrep")
        rule_kind = None
        if a.rule:
            txt = Path(a.rule).read_text(encoding="utf-8", errors="replace")
            rule_kind = "semgrep" if re.search(r"^rules\s*:", txt, re.M) else "ast-grep"
        if ag and (a.ast or rule_kind == "ast-grep"):
            hits = ast_grep_search(ag, root, a.lang, a.ast, a.rule, excludes)
            notes.append(f"structural: ast-grep ({len(hits)} hits)")
            results += hits
        elif sem and ((a.rule and rule_kind == "semgrep") or (a.ast and LANGS[a.lang][1])):
            hits = semgrep_search(sem, root, a.lang, a.ast, a.rule if rule_kind == "semgrep" else None, excludes)
            notes.append(f"structural: semgrep ({len(hits)} hits)")
            results += hits
        else:
            why = "no ast-grep/semgrep on PATH" if not (ag or sem) else f"no structural tool supports this input for {a.lang}"
            notes.append(f"structural: skipped ({why}) -- relying on text search; expect more false positives")
            if not a.regex and not a.symbol and a.ast:
                # derive a rough regex from the pattern's leading identifier chain
                m = re.match(r"\s*([\w.:]+)\s*\(", a.ast)
                if m and not m.group(1).startswith("$"):
                    a.regex.append(re.escape(m.group(1)) + r"\s*\(")
                    notes.append(f"derived regex from --ast: {a.regex[-1]}")

    # text search
    for rx in a.regex:
        hits, tool = text_search(root, a.lang, rx, excludes, a.fixed_strings, a.ignore_case, a.hidden)
        notes.append(f"text: {tool} /{rx}/ ({len(hits)} hits)")
        results += hits

    # symbol search
    for name in a.symbol:
        sym_ex = excludes if a.include_tests else excludes + ["test", "tests", "__tests__", "spec", "*.test.*", "*.spec.*", "test_*.py", "*_test.*"]
        hits, tool = text_search(root, a.lang, rf"\b{re.escape(name)}\b", sym_ex)
        classify_symbol(hits, name)
        nd = sum(h["tag"] == "def" for h in hits)
        ni = sum(h["tag"] == "imp" for h in hits)
        notes.append(f"symbol {name}: {tool} ({nd} defs, {len(hits) - nd - ni} refs, {ni} imports hidden; "
                     f"tests {'included' if a.include_tests else 'skipped'})")
        results += [h for h in hits if h["tag"] != "imp" or a.show_imports]

    # dedupe (prefer the more precise tag)
    rank = {"ast": 0, "def": 1, "ref": 2, "text": 3, "imp": 4}
    best: dict[tuple, dict] = {}
    for h in results:
        key = (os.path.normpath(h["file"]), h["line"])
        if key not in best or rank[h["tag"]] < rank[best[key]["tag"]]:
            h["file"] = key[0]
            best[key] = h
    hits = sorted(best.values(), key=lambda h: (h["file"], h["line"]))
    fill_text(root, hits)

    if a.json:
        print(json.dumps({"notes": notes, "count": len(hits), "hits": hits[: a.max]}, indent=2))
        return 0

    for n in notes:
        print(f"# {n}")
    if not hits:
        print("\nNo candidates. Before concluding there are no siblings, try the other two searches"
              " (same shape / same function / same assumption) and a looser pattern.")
        return 0
    files = {}
    for h in hits[: a.max]:
        files.setdefault(h["file"], []).append(h)
    for f, hs in files.items():
        print(f"\n{f}")
        for h in hs:
            print(f"  {h['line']:>5}  [{h['tag']}]  {h['text'][:160]}")
    print(f"\n{len(hits)} candidate(s) in {len({h['file'] for h in hits})} file(s).", end=" ")
    if len(hits) > a.max:
        print(f"Showing {a.max}; narrow the pattern.", end=" ")
    if len(hits) > 30:
        print("That's a lot -- narrow the pattern before verifying.", end=" ")
    print("Verify each one before calling it a bug.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into `head` etc.
        sys.exit(0)
