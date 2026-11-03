# Bug class catalog (cross-stack)

Each class is defined by the **assumption that broke**, because that's what you search for when hunting siblings. Stack-specific search commands and guards live in `js-ts.md`, `python.md`, `luau.md`, `c-cpp.md`.

Every entry has: **Assumption** (what the code wrongly believed) · **Looks like** (typical symptoms) · **Hunt** (the three searches: shape / function / assumption) · **Verify** (the question that separates a real sibling from a look-alike) · **Guard** (what stops the whole class).

## Contents
- Async & state: [missing-await](#missing-await) · [stale-closure-or-state](#stale-closure-or-state) · [race-condition](#race-condition) · [shared-mutable-default](#shared-mutable-default)
- Data & contracts: [unchecked-null](#unchecked-null) · [response-shape-mismatch](#response-shape-mismatch) · [wrong-type-or-unit](#wrong-type-or-unit) · [falsy-zero](#falsy-zero)
- Logic: [off-by-one](#off-by-one) · [missing-corner-case](#missing-corner-case) · [misread-requirement](#misread-requirement) · [duplicated-logic-drift](#duplicated-logic-drift)
- Errors & environment: [swallowed-error](#swallowed-error) · [env-config-drift](#env-config-drift) · [resource-leak](#resource-leak)
- AI-specific: [hallucinated-api-or-package](#hallucinated-api-or-package)
- Security & memory: [unsafe-input](#unsafe-input) · [memory-safety](#memory-safety)
- [Naming a new class](#naming-a-new-class)

---

### missing-await
**Assumption:** an async operation has finished by the next line.
**Looks like:** intermittent "not found" right after a save, values that are `Promise {<pending>}` / a coroutine object, writes that "sometimes don't stick", unhandled rejection warnings, tests that pass alone and fail together.
**Hunt:** shape — calls to known-async functions not preceded by `await`/`return`/`yield`; async callbacks passed to things that don't wait (`forEach`, `map` without `Promise.all`, event emitters, `setTimeout`). Function — every caller of the async function you fixed. Assumption — other code that reads the same record/state right after triggering a write.
**Verify:** is the result or side effect needed before the promise settles? Is the promise returned to (and awaited by) a caller? Is it a deliberate fire-and-forget with its own error handling?
**Guard:** a no-floating-promises lint rule; async return types visible in signatures; a test that reads immediately after writing.

### stale-closure-or-state
**Assumption:** a callback sees the current value of a variable or state.
**Looks like:** UI shows old data, counters stuck at the first value, handlers using the props/state from the first render, loop callbacks all seeing the last loop value.
**Hunt:** shape — callbacks/effects/timers/subscriptions created once (empty dependency lists, registered in init) that read mutable state; closures created in loops. Function — other effects or handlers using the same state. Assumption — other places reading the same state inside long-lived callbacks.
**Verify:** does the value change after the callback is created, and does the callback run after that change?
**Guard:** exhaustive-deps style lint rule; functional state updates; capture the loop variable by value; late-binding lint (Python B023).

### race-condition
**Assumption:** two operations on shared state happen in a fixed order, or one at a time.
**Looks like:** rare duplicates, lost updates, double-spends, "works on my machine", failures under load or on fast/slow networks.
**Hunt:** shape — read-modify-write on shared state without a lock/transaction/atomic op; check-then-act (`if not exists: create`). Function — all writers of the same record/table/global. Assumption — other code assuming exclusive access (same key, same file, same DataStore entry).
**Verify:** can two executions interleave between the read and the write? (Two requests, two players, two threads, two tabs.)
**Guard:** atomic operations, transactions, unique constraints, idempotency keys, a mutex, or `UpdateAsync`-style compare-and-set. Concurrency fixes are wrong far more often than other fixes — test with forced interleavings (delays/barriers), not just one happy run.

### shared-mutable-default
**Assumption:** each object/call gets its own fresh copy of a default table/list/object.
**Looks like:** data leaking between users/players/instances, a list that keeps growing across calls, "everyone has the same inventory".
**Hunt:** shape — mutable default arguments; module-level tables/objects assigned into instances; shallow copies (`{...obj}`, `Object.assign`, `table.clone`, `dict.copy()`, `copy.copy`) of nested structures. Function — every constructor/factory using the same defaults table. Assumption — other places that copy a template and then mutate a nested field.
**Verify:** is the nested structure later mutated through one instance and read through another?
**Guard:** create defaults inside the function; deep copy; freeze templates (`Object.freeze`, `table.freeze`); Python lint B006/B008.

### unchecked-null
**Assumption:** a lookup always finds something (`null`/`None`/`nil`/`undefined` is impossible).
**Looks like:** "Cannot read properties of undefined", `'NoneType' object is not subscriptable`, "attempt to index nil", segfault on NULL.
**Hunt:** shape — results of find/get/lookup/query/`FindFirstChild`/`malloc`/`dict.get`/`Array.find` used without a check. Function — every caller of the lookup that returned null. Assumption — other functions with the same "may return nothing" contract used the same way.
**Verify:** can this specific call return nothing for real input (missing id, deleted record, slow-loading instance)? Is it checked upstream?
**Guard:** make absence explicit in the type (`T | undefined`, `Optional[T]`, `T?`) with strict checking on; a `get_or_raise`/`require` helper with a clear error; validation at the boundary.

### response-shape-mismatch
**Assumption:** external data (API response, JSON, DB row, file, user input) has the shape the code expects.
**Looks like:** `undefined` fields after an API change, `data.items` vs `data.results`, a list where an object was expected, pagination wrappers, string numbers.
**Hunt:** shape — direct property access on parsed JSON/responses without validation. Function — every consumer of the same endpoint/client/table. Assumption — every place that reads the same field name.
**Verify:** what does the real response look like right now? (Capture one; don't trust docs or the model's memory.)
**Guard:** parse and validate once at the boundary (schema validation, typed DTOs, dataclasses/pydantic, zod); a contract test with a recorded real response.

### wrong-type-or-unit
**Assumption:** a value is the type or unit the code thinks (number vs string, ms vs s, cents vs dollars, UTC vs local, 0- vs 1-based).
**Looks like:** `"10" + 1 = "101"`, timers 1000x off, prices 100x off, dates a day off, sort order "1, 10, 2".
**Hunt:** shape — arithmetic/comparison on values from input/env/JSON; default `sort()`; date constructors. Function — all producers/consumers of the same field. Assumption — same field name or same unit across the codebase.
**Verify:** trace the value from where it's created to where it's used; print its type at both ends.
**Guard:** parse at the boundary; unit-suffixed names (`timeoutMs`, `priceCents`); typed wrappers; explicit comparators.

### falsy-zero
**Assumption:** "falsy" means "missing" (so `0`, `""`, `False`, or an empty list gets treated as absent).
**Looks like:** quantity 0 replaced by a default, an empty search returning everything, page 0 ignored. (In Lua/Luau, `0` and `""` are truthy — the inverse mistake.)
**Hunt:** shape — `x || default`, `if (!x)`, `if not x:`, `x or default` where `x` can legitimately be 0/empty. Function/assumption — other uses of the same field.
**Verify:** can the value legitimately be zero/empty here?
**Guard:** explicit null checks (`??`, `is None`, `== nil`); tests with 0/empty inputs.

### off-by-one
**Assumption:** a range, index, or count starts/ends where the code thinks (inclusive vs exclusive, 0- vs 1-based, length vs last index).
**Looks like:** first/last item missing or duplicated, pagination skipping a page, buffer overruns, fencepost errors in loops.
**Hunt:** shape — `<=` with `length`/`#t`, `slice/substring` math, `page * size`, `len - 1`, manual loops over ranges. Function — every copy of the same index math (often copy-pasted with tweaks). Assumption — anything else using the same convention (e.g. every place that receives `page` from the UI).
**Verify:** run the boundary cases: empty, one item, exactly a page, one past a page.
**Guard:** one shared helper for the math; boundary tests; iterate with built-ins instead of index math.

### missing-corner-case
**Assumption:** inputs are "normal" (non-empty, ASCII, positive, in the same timezone, no duplicates).
**Looks like:** crashes on empty lists, division by zero, unicode/emoji breaking string slicing, DST/timezone bugs, duplicates double-counted.
**Hunt:** shape — division, `[0]`/`[-1]`, `max()/min()` on possibly empty collections, string slicing, date math. Function/assumption — other handlers of the same input type.
**Verify:** can that input actually arrive here?
**Guard:** property-based or table-driven tests over edge values; guards at the boundary.

### misread-requirement
**Assumption:** the code implements what was asked — but it implements something plausible instead (common in generated code).
**Looks like:** works, but the behavior is wrong in a way no exception reveals: wrong rounding, wrong filter, inclusive where exclusive was asked.
**Hunt:** function — other code generated from the same prompt/spec; assumption — every place implementing the same business rule.
**Verify:** compare against the spec, README, ticket, or the user — not against what the code currently does. If the spec is unclear, ask.
**Guard:** a test written from the requirement's wording; one shared implementation of the rule.

### duplicated-logic-drift
**Assumption:** copies of the same logic stay in sync. They don't — one gets fixed, the others don't.
**Looks like:** a bug fixed in one screen reappearing in another; slightly different validation in two endpoints.
**Hunt:** search for a distinctive line, constant, regex, error message, or magic number from the buggy block — copies get renamed but keep their guts. Also search the identifier names used inside the block.
**Verify:** is it really the same logic with the same intent, or a deliberate variant?
**Guard:** extract one shared function and replace copies (this is a refactor: ask first); a test on the shared function.

### swallowed-error
**Assumption:** an error can be safely ignored (or logged and forgotten).
**Looks like:** silent failures, "it says saved but nothing saved", empty results instead of an error, missing logs.
**Hunt:** shape — empty catch, `except: pass`, `except Exception` that returns a default, ignored `pcall` results, `.catch(() => {})`, ignored C return codes. Function — other callers of the same failing operation. Assumption — other places where this operation's failure is treated as success.
**Verify:** what should happen when this fails — retry, surface to the user, abort? Is ignoring it documented and intended (e.g. best-effort cleanup)?
**Guard:** lint rules against empty catch / bare except / try-except-pass; return or raise typed errors; `[[nodiscard]]`.
**Note:** the fix is to *stop* swallowing — never to add more swallowing elsewhere.

### env-config-drift
**Assumption:** the environment matches the code (same env var names, same config keys, same versions, same feature flags across dev/prod).
**Looks like:** works locally, fails in prod (or vice versa); `undefined` config values; a renamed env var that half the code still reads by the old name.
**Hunt:** shape — every read of env/config (`process.env.`, `os.environ`, `getenv`, settings objects). Assumption — every reader of the same key and every file declaring it (`.env.example`, deploy config, CI, docs).
**Verify:** where is this key set in each environment? Print the effective config at startup.
**Guard:** one config module that reads and validates all keys at startup and fails loudly; `.env.example` kept in sync; no direct env reads elsewhere.

### resource-leak
**Assumption:** something cleans up after itself (connections, files, listeners, timers, event connections).
**Looks like:** memory growth, "too many open files", handlers firing multiple times, lag that grows over a session.
**Hunt:** shape — open/connect/subscribe/`addEventListener`/`:Connect` without a matching close/unsubscribe/`Disconnect`; effects without cleanup. Function/assumption — other subscribers to the same source.
**Verify:** does the owner get destroyed or re-created while the resource lives on?
**Guard:** context managers / `using` / RAII / cleanup returns / a maid-janitor pattern; a test that creates and destroys many times.

### hallucinated-api-or-package
**Assumption:** a function, method, option, or package exists because the code (often AI-written) uses it.
**Looks like:** `ModuleNotFoundError`, "is not a function", unknown option errors, a package name that's *almost* right, docs that don't mention the method.
**Hunt:** shape — every import/require of the same module; every call of the same invented method/option. Assumption — other code written in the same session or style using the same API.
**Verify:** check the real API: the installed package's source or type definitions, the official docs, or the registry. For packages: does the name exist in the registry, is it the well-known package (download counts, repository link), and does the project already depend on something that provides this? A registry hit is **not** proof — attackers register commonly-hallucinated names ("slopsquatting"), and some real packages have near-identical names to the one intended.
**Guard:** pin dependencies with a lockfile; type checking that fails on unknown members; never install a new package without the user's OK.
**Fix:** switch to the real API that does the job (often stdlib or an existing dependency). Don't add a dependency just to make an import resolve.

### unsafe-input
**Assumption:** input is trustworthy (from your own UI, your own client, a well-formed file).
**Looks like:** injection (SQL, shell, HTML/XSS), path traversal, prototype pollution, game clients sending fake values (Roblox RemoteEvents), auth checks only on the client.
**Hunt:** shape — string-built queries/commands/HTML; file paths from input; trust of client-sent values on the server. Function — every handler of the same endpoint/remote. Assumption — every place the same input flows to.
**Verify:** trace whether an attacker controls the value end to end.
**Guard:** parameterized queries, argument arrays instead of shell strings, output encoding, server-side validation, allow-lists. Security bugs are always T2.

### memory-safety
**Assumption:** sizes, lifetimes, and terminators are right (C/C++).
**Looks like:** crashes far from the bug, garbage strings, sanitizer reports, heisenbugs that vanish under the debugger.
**Hunt:** shape — `strncpy`/`strcpy`/`sprintf`/`memcpy` with computed lengths, manual index math, `free` followed by use, returning pointers to locals. Function — every caller passing buffers to the same function. Assumption — same buffer size constant used elsewhere.
**Verify:** compute the sizes for the boundary input; run under AddressSanitizer/UBSan.
**Guard:** `-fsanitize=address,undefined` in tests; safer APIs (`snprintf`, bounded copies that always terminate, `std::string`, spans); compiler warnings as errors.

---

### Naming a new class
If nothing fits, name the **broken assumption**, not the symptom: `assumes-sorted-input`, not `wrong-order-bug`. Kebab-case, 2–4 words. Write the Assumption/Hunt/Verify/Guard lines into the bug log entry so the next session can reuse them.
