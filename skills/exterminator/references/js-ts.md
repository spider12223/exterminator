# JavaScript / TypeScript / React / Next.js

Search commands assume you're at the repo root; `FV` = `python3 <skill>/scripts/find_variants.py`. Use `--lang ts` for `.ts`, `--lang tsx` for `.tsx`, `--lang js` for `.js/.jsx/.mjs`.

## Contents
- [Hunting recipes by class](#hunting-recipes-by-class)
- [React-specific](#react-specific)
- [Next.js-specific](#nextjs-specific)
- [Classic JS traps](#classic-js-traps)
- [Class guards](#class-guards)
- [Checking a package is real](#checking-a-package-is-real)

## Hunting recipes by class

**missing-await**
```bash
FV --lang js --symbol saveOrder                       # every caller; check each for await/return
FV --lang js --rule unawaited.yml                     # calls not under await/return (rule below)
FV --lang js --ast '$A.forEach(async ($$$P) => { $$$B })' --regex 'forEach\(\s*async'
FV --lang js --regex '\.then\(' --regex 'new Promise\('   # mixed styles hide missing awaits
```
`unawaited.yml` (save in a temp dir or `.claude/rules/`; change the function name):
```yaml
id: unawaited-call
language: javascript   # or typescript / tsx
rule:
  pattern: saveOrder($$$ARGS)
  not:
    inside:
      any:
        - kind: await_expression
        - kind: return_statement
        - kind: arrow_function
```
Hits inside `Promise.all([...])`, assigned to a variable that's awaited later, or deliberately fire-and-forget with `.catch(...)` are usually fine — verify.

**unchecked-null**: `.find(`, `.get(`, `querySelector(`, `getElementById(`, `JSON.parse(` results, `?.` followed by a non-optional use, `!` non-null assertions in TS.
```bash
FV --lang ts --regex '\.find\([^)]*\)\.' --regex '!\.'      # find(...).prop and x!.prop
```

**response-shape-mismatch**: every `await res.json()` / `axios` `.data` consumer of the same endpoint; grep the endpoint path string and the field name.
```bash
FV --lang ts -F --regex '/api/orders' ; FV --lang ts --regex '\.(data|items|results)\b'
```

**swallowed-error**
```bash
FV --lang js --regex 'catch\s*(\([^)]*\))?\s*\{\s*\}' --regex '\.catch\(\s*\(?\w*\)?\s*=>\s*(\{\s*\}|null|undefined)'
FV --lang js --regex 'catch\s*\(\s*\w+\s*\)\s*\{\s*(console\.(log|error)\([^)]*\);?\s*)?(return\s+(null|undefined|\[\]|\{\}|false);?)?\s*\}'
```

**env-config-drift**
```bash
FV --lang js --regex 'process\.env\.' --regex 'import\.meta\.env\.'
FV --hidden --regex 'API_URL|API_BASE_URL'                  # no --lang: include .env*, CI, Dockerfiles
```

**off-by-one / duplicated-logic-drift**: search the arithmetic itself, not the function name — copies get renamed.
```bash
FV --lang js --regex '\*\s*(page|pageSize|size|limit)\b' --regex 'slice\(\s*\w+\s*\*'
```

**falsy-zero**: `FV --lang js --regex '\|\|\s*(0|\d+|\x27\x27|""|\[\]|\{\})' --regex 'if\s*\(\s*!\w+(\.\w+)*\s*\)'` then check whether 0/"" are valid values (use `??` instead of `||`).

## React-specific

- **Stale closures**: `useEffect`/`useCallback`/`useMemo` with missing deps; `setInterval`/subscriptions created in an effect with `[]` that read state. Fix with correct deps or functional updates (`setCount(c => c + 1)`), not by silencing `react-hooks/exhaustive-deps`.
- **State updates assumed synchronous**: reading state right after `setX(...)` expects the new value — it won't have it.
- **Effects without cleanup**: listeners, intervals, subscriptions, fetches without abort → leaks and double handlers (worse in StrictMode, which double-invokes effects in dev).
- **List keys from index** on reorderable lists → state attached to the wrong row.
- **Derived state copied into state** (`useState(props.x)`) → doesn't update when props change.
```bash
FV --lang tsx --ast 'useEffect($$$, [])' --regex 'useEffect\('
FV --lang tsx --regex 'key=\{\s*(i|index|idx)\s*\}'
```

## Next.js-specific

- **Env var exposure/drift**: only `NEXT_PUBLIC_*` reaches the browser; a renamed var breaks one side silently. Search both the server and client trees.
- **Server/client boundary**: hooks or browser APIs in Server Components; secrets imported into `'use client'` files; non-serializable props passed to client components.
- **Caching assumptions**: `fetch` caching and route segment config (`dynamic`, `revalidate`) — "data never updates" is often a cache, not a logic bug. Check the Next.js version's defaults before changing anything.
- **Route handler shape**: handlers returning plain objects instead of `Response`/`NextResponse`; reading `req.body` App-Router style vs Pages-Router style.
```bash
FV --lang tsx -F --regex "'use client'" ; FV --lang ts --regex 'process\.env\.(?!NEXT_PUBLIC_)'
```

## Classic JS traps

`==` coercion · `parseInt` without radix · default `Array.prototype.sort()` is lexicographic (`[10, 9, 1].sort()` → `[1, 10, 9]`) · `new Date(y, m, d)` months are 0-based · floating-point money (use integer cents) · `for...in` over arrays · `this` lost in callbacks · `JSON.parse` on untrusted input without try/catch at the boundary · object spread is shallow (shared-mutable-default).

## Class guards

| Class | Guard |
|---|---|
| missing-await | `@typescript-eslint/no-floating-promises`, `@typescript-eslint/no-misused-promises`, `require-await` (typed linting needs `parserOptions.project`) |
| unchecked-null | `"strict": true` (includes `strictNullChecks`), `noUncheckedIndexedAccess` |
| response-shape-mismatch | schema validation at the fetch boundary (zod/valibot/io-ts) with inferred types |
| stale-closure | `react-hooks/exhaustive-deps` as an error |
| swallowed-error | `no-empty` (with `allowEmptyCatch: false`), a custom rule or ast-grep rule for `.catch(() => {})` |
| env-config-drift | one `config.ts` that reads and validates every env var at startup; no `process.env` elsewhere (enforce with `no-restricted-properties` or an ast-grep rule) |
| duplicated-logic-drift | extract the shared function; ast-grep rule banning the old inline pattern |

Adding a lint plugin is a new dev dependency: ask first. If the project already has ESLint/TS configured, enabling a rule is usually fine — mention it in the report.

## Checking a package is real

```bash
npm view <name> name version repository.url time.created   # exists? who publishes it? how old?
npm ls <name>                                              # is it already installed/depended on?
```
Red flags: created very recently, no repository, near-miss of a popular name, very low downloads, the code's usage doesn't match its README. If the import is invented, replace it with the real API (often a built-in or an existing dependency) instead of installing anything.
