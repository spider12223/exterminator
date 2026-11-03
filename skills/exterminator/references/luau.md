# Luau / Roblox

`FV` = `python3 <skill>/scripts/find_variants.py --lang luau`.

**Tooling reality:** Semgrep doesn't scan `.luau` files. ast-grep has no Luau grammar, but `find_variants.py --lang luau` maps `*.luau` onto ast-grep's Lua grammar, which recovers from Luau type annotations well enough for call/index patterns. Always pair `--ast` with a `--regex`, because annotated code can still parse oddly. Verification tools: `luau-analyze` (type check + lint), `luau-lsp analyze` (with Roblox definitions and a Rojo sourcemap), Selene (`std = "roblox"`). Tests run in Studio (TestEZ / Jest-Lua) or outside it with a standalone Luau runtime such as Lune for non-Roblox-API code.

## Contents
- [Hunting recipes by class](#hunting-recipes-by-class)
- [Roblox-specific traps](#roblox-specific-traps)
- [Class guards](#class-guards)

## Hunting recipes by class

**unchecked-null** — instance lookups and table lookups that can return `nil`
```bash
FV --ast '$A:FindFirstChild($$$)' --regex ':FindFirstChild\w*\(' --regex ':FindFirstAncestor\w*\('
FV --regex ':WaitForChild\([^,)]+,\s*[\d.]+\)'          # WaitForChild WITH a timeout can return nil
FV --regex 'GetPlayerFromCharacter\(' --regex '\.Character\b'
FV --regex 'Inventory\.getItem\([^)]*\)\.'              # result indexed immediately (adapt the name)
```
Verify: can the instance/key be missing at that moment (not replicated yet, destroyed, character respawning, player left)?

**shared-mutable-default** — the #1 "everyone shares the same inventory" bug
```bash
FV --regex 'setmetatable\(\s*\{\s*\}\s*,\s*\{\s*__index\s*=' # writes to nested tables go to the shared template
FV --regex 'table\.clone\('                               # shallow: nested tables still shared
FV --regex '=\s*DEFAULT\w*\.\w+'                          # instance field pointing at a template's table
FV --regex '^local\s+\w+\s*=\s*\{\s*\}\s*$'               # module-level state shared by every caller
```
Verify: is the nested table later mutated via one instance and read via another?

**swallowed-error** — especially around DataStores, where it means silent data loss
```bash
FV --regex '^\s*pcall\(' --regex '^\s*task\.spawn\(\s*pcall' --regex 'local\s+_\s*=\s*pcall\('
FV --regex 'local\s+\w+\s*,\s*\w+\s*=\s*pcall\(' --regex 'xpcall\('
```
Check the line after each `pcall`: is the success flag checked, and does failure do something visible (retry, kick with a message, block saving)?

**resource-leak** — connections that outlive their owner
```bash
FV --regex ':Connect\(' --regex ':Once\(' ; FV --regex ':Disconnect\(\)'
FV --regex 'RunService\.(Heartbeat|RenderStepped|Stepped):Connect'
```
Compare: connections created per player/character/round vs disconnects. Every `PlayerAdded`/`CharacterAdded` connection that captures the player needs cleanup.

**unsafe-input** — trusting the client
```bash
FV --regex 'OnServerEvent:Connect' --regex 'OnServerInvoke\s*='
```
For each handler: are argument types, ranges, ownership, and rate checked on the server? Remember the first argument is always the player — handlers that forget it shift every argument by one.

**off-by-one** — 1-based indexing
```bash
FV --regex 'for\s+\w+\s*=\s*0\s*,' --regex '#\w+\s*-\s*1' --regex '\[\s*0\s*\]' --regex 'string\.sub\('
```

**race-condition**
```bash
FV --regex ':SetAsync\(' --regex ':GetAsync\(' ; FV --regex 'PlayerRemoving' --regex 'BindToClose'
```
`GetAsync` → modify → `SetAsync` loses concurrent writes; use `UpdateAsync`. Saving on `PlayerRemoving` without `BindToClose` loses data on shutdown.

**hallucinated-api** — invented Roblox APIs and properties are common in generated code. Check the Roblox Creator Docs API reference or the type definitions loaded by luau-lsp; `luau-analyze`/`luau-lsp analyze` flag unknown members on typed instances.

## Roblox-specific traps

- `.` vs `:` method calls: `obj.method(x)` passes `x` as `self`. Symptom: "attempt to index nil" or arguments off by one.
- Truthiness is the inverse of JS: only `nil` and `false` are falsy, so `0` and `""` are truthy. `if count then` doesn't catch zero.
- `#t` on tables with holes is unreliable; `ipairs` stops at the first `nil`.
- Numeric vs string keys: `t[1]` ≠ `t["1"]`; DataStores and JSON can turn sparse numeric keys into strings.
- `wait()`/`spawn()`/`delay()` are deprecated; use `task.wait`/`task.spawn`/`task.delay`.
- Client replication timing: `FindFirstChild` on the client right after join may return `nil`; `WaitForChild` without a timeout can hang forever (and warns).
- Character lifecycle: references to `player.Character` go stale on respawn; re-fetch on `CharacterAdded`.
- Server vs client authority: client-side checks (cooldowns, prices, damage) are suggestions; the server must enforce them.

## Class guards

| Class | Guard |
|---|---|
| unchecked-null / wrong-type | `--!strict` at the top of the module (or `"languageMode": "strict"` in `.luaurc`); annotate `-> Tool?` so callers must check |
| shared-mutable-default | build defaults inside the constructor (a `defaults()` function); deep copy helper; `table.freeze` templates so accidental writes error |
| swallowed-error | a `safeCall`/retry helper that returns typed results and logs/handles failure; never a bare `pcall(...)` statement |
| resource-leak | Maid/Janitor/Trove cleanup objects owned by the player/round |
| unsafe-input | one validation layer for all remotes (types, ranges, rate limits, ownership) |
| race-condition | `UpdateAsync`, session locking, `BindToClose` |
| lint | Selene with `std = "roblox"`; `luau-lsp analyze` in CI |
