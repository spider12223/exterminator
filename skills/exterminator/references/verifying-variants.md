# Verifying sibling candidates

A search hit is a lead, not a bug. Reporting look-alikes as bugs wastes the user's trust; "fixing" them can break working code. Every candidate gets a verdict with a reason.

## The checklist (per candidate)

1. **Read it in context.** Open the function, not just the matched line. Check the callers one level up.
2. **Same broken assumption?** State the assumption from your cause chain and ask whether it's false *here too*. Same syntax with a different contract is not a sibling.
3. **Reachable?** Is the code live (exported, called, routed, bound to an event)? Dead code is "not a bug (unreachable)" — mention it, don't fix it.
4. **Already handled?** Guarded upstream (null check, validation, `await` in the caller), caught downstream, or impossible by construction (value just created, constant input)?
5. **Same consequences?** What would the user see if it fails here? This sets the priority in your report.
6. **Prove it when cheap.** A two-minute failing test turns "suspected" into "confirmed" and becomes the regression test.

## Verdicts

- **confirmed** — you showed it fails (test, script, or a trace with concrete values). Fix or ask, per the rules in SKILL.md step 8.
- **suspected** — the pattern and assumption match but you couldn't prove it (needs a real environment, external service, specific data). Report under "Couldn't verify" with what would settle it. Don't fix silently.
- **not a bug** — give the one-line reason: "awaited by caller", "id is created two lines above", "documented fire-and-forget with .catch(logError)", "unreachable: not exported".

## False-positive traps

- **Deliberate fire-and-forget**: un-awaited async call with its own error handling and no dependent reads.
- **Guarded upstream**: the null check, validation, or `await` lives in the caller.
- **Different contract, same name**: two `getUser` functions in different modules, one returning `None` and one raising.
- **Test fixtures, mocks, examples, generated code, vendored code**: matches there are usually not product bugs. Note them only if they hide a real problem (e.g. a mock that returns a shape the real API never does).
- **Already fixed in this session**: don't double-count the seed site.
- **Intentional variant**: copied logic that was changed on purpose (check git blame/commit messages before "re-syncing" it).

## Missed-sibling traps (false negatives)

- **Renamed copies**: search for a distinctive inner line, constant, or error string — not only the function name.
- **Different syntax, same assumption**: `forEach(async ...)` is the same bug as a missing `await`; `table.clone(defaults)` is the same bug as assigning `defaults.items` directly.
- **Other languages in the repo**: the same contract may be consumed by the frontend and the backend, or a Python script and a Luau module.
- **Config and data files**: env var names in `.env.example`, CI, Dockerfiles, deploy configs.
- **The bug log**: rerun stored searches for the same class.

## Too many candidates

More than ~30 hits means the pattern is too broad. Narrow it (add the receiver, the argument shape, a containing context), split by directory, or verify a sample and report "N similar sites, sampled M, all M are real: recommend fixing the shared source" — then ask.
