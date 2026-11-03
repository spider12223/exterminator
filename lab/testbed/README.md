# ShopKit

A small multi-language shop backend used for prototyping. Product rules:

- **Pages are 1-based** everywhere (UI, API, admin): `page=1` is the first page.
- **Discount codes:** `SAVE10` takes 10% off the order total. Unknown codes change nothing.
- **Deleted users** display as `Deleted user` on profile pages. Billing/report code must never crash on a
  missing user; raising `UserNotFound` is fine.
- **Sync and imports must never hide failures.** If an upload, insert, or webhook delivery fails, the caller
  has to find out (raise `SyncError` or report a failure status) — silent success is a data-loss bug.
- **Config:** the backend base URL comes from the `API_BASE_URL` environment variable (it used to be
  called `API_URL`). See `js/.env.example`.
- **Python is stdlib-only** (3.11+). No third-party packages.
- **Names in the C module are truncated** to `NAME_LEN - 1` characters and are always NUL-terminated.
  The same applies to item titles and tags.
- **Game (Luau):** every player, pet, and quest has its own independent state.

## Running tests

    cd js && node --test
    cd py && python3 -m unittest discover -s tests -t .
    luau luau/tests/run.luau
    cd c && make test
