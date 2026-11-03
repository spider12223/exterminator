# Python

`FV` = `python3 <skill>/scripts/find_variants.py --lang python`.

## Contents
- [Hunting recipes by class](#hunting-recipes-by-class)
- [Classic Python traps](#classic-python-traps)
- [Class guards](#class-guards)
- [Checking a package or API is real](#checking-a-package-or-api-is-real)

## Hunting recipes by class

**unchecked-null** (functions that can return `None`)
```bash
FV --symbol get_user                                   # every caller of the lookup
FV --regex 'get_user\([^)]*\)\s*(\[|\.)'              # result used directly
FV --regex '\.get\([^,)]+\)\s*(\[|\.)'                 # dict.get(...) then indexed
FV --regex 're\.(match|search|fullmatch)\([^)]*\)\.'  # regex match used without a None check
```
Also search for other functions with the **same contract** ("returns None if not found") — they're siblings even with a different name:
```bash
FV -i --regex='->\s*(Optional\[|.*\|\s*None)' --regex 'or None\b|None if (there|no|not)'   # annotations + docstrings
```

**swallowed-error**
```bash
FV --regex '^\s*except\s*:' --regex '^\s*except\s+(Base)?Exception\b'
FV --ast 'try:
    $$$BODY
except $E:
    pass'
```
For each hit: what should happen on failure? Narrow the exception type and re-raise, log-and-raise, or return a typed error — never a silent default the caller can't distinguish from success.

**shared-mutable-default**
```bash
FV --regex 'def \w+\([^)]*=\s*(\[\]|\{\}|set\(\)|dict\(\)|list\(\))'
FV --regex '^\s*\w+\s*=\s*(\[\]|\{\})\s*$'            # module-level containers shared by everyone
FV --regex 'copy\.copy\(|\.copy\(\)'                   # shallow copies of nested structures
```

**missing-await (asyncio)**: "coroutine ... was never awaited" warnings; `asyncio.create_task` results not kept (the task can be garbage-collected); sync calls of async functions.
```bash
FV --symbol fetch_order ; FV --regex 'asyncio\.create_task\(' ; FV --regex '^\s*(?!await\b|return\b)\w[\w.]*\.(save|fetch|send|write)_?\w*\('
```

**stale closure / late binding**: lambdas or inner functions in loops capturing the loop variable.
`FV --regex 'lambda[^:]*:\s*.*\b(i|item|x|row)\b'` then check whether it's created in a loop and called later.

**env-config-drift**
```bash
FV --regex 'os\.environ(\.get)?\s*[\[(]' --regex 'os\.getenv\(' --regex 'settings\.\w+'
FV --hidden --regex 'DATABASE_URL|DB_URL'    # without --lang, to include .env*, CI, Docker files
```

**hallucinated-api-or-package**
```bash
FV --regex '^\s*(from|import)\s+dateutils\b'           # every import of the suspect module
python3 -c "import importlib.util,sys; print(importlib.util.find_spec(sys.argv[1]))" dateutils
python3 -c "import datetime; print(hasattr(datetime.datetime, 'fromisoformat'))"   # does the method exist?
```

**response-shape-mismatch**: every consumer of the same endpoint or JSON field: `FV -F --regex '"/api/orders' ; FV --regex "\[['\"]results['\"]\]"`.

**off-by-one / duplicated-logic-drift**: `FV --regex '\*\s*(page|page_size|size|limit)\b' --regex 'range\(\s*1\s*,' --regex 'len\(\w+\)\s*-\s*1'`.

## Classic Python traps

`is` vs `==` for numbers/strings · integer vs float division (`/` vs `//`) · naive vs aware datetimes (and `datetime.utcnow()` returning naive) · `dict.get(k, default)` hiding typos in keys · iterating a dict while mutating it · `str.strip("abc")` strips characters, not a prefix (use `removeprefix`) · `requests` without `timeout` hanging forever · bare `except:` also catching `KeyboardInterrupt` and `SystemExit` · string-formatted SQL.

## Class guards

| Class | Guard |
|---|---|
| unchecked-null | annotate `-> X \| None`; `mypy --strict` or pyright `strict`; a `get_x_or_raise()` helper |
| swallowed-error | ruff `E722` (bare except), `BLE001` (blind except), `S110` (try-except-pass), `SIM105` reviewed manually |
| shared-mutable-default | ruff `B006`, `B008` |
| late binding | ruff `B023` |
| missing-await | `RUF006` (dangling task), `PYTHONASYNCIODEBUG=1` / `-W error::RuntimeWarning` in tests |
| naive datetimes | ruff `DTZ` rules |
| response-shape | pydantic / dataclasses / TypedDict validation at the boundary |
| env-config-drift | one `config.py` (pydantic-settings or plain dataclass) that validates at import; ban `os.environ` elsewhere |
| injection | ruff `S608` (SQL), `S602`/`S603` (subprocess shell) |

If the project already uses ruff/mypy, enabling a rule is cheap. Adding a new tool is a new dev dependency: ask.

## Checking a package or API is real

```bash
python3 -m pip index versions <name>        # exists on PyPI? (needs network)
python3 -m pip show <name>                   # installed? what does it claim to be?
python3 -c "import <module>; print(<module>.__file__)"
```
PyPI distribution names and import names can differ (`python-dateutil` installs `dateutil`; `beautifulsoup4` installs `bs4`). An import of `dateutils` is not a reason to install `dateutils` — find what the code meant (often the stdlib) and use that. Never `pip install` an unverified name.
