# Bug patterns

Written by the exterminator skill. Each entry is a bug class found in this
project, the assumption that broke, the search that finds its siblings, and
the guard that now prevents it. Future sessions read this first and rerun the
stored searches to catch new copies early. Keep entries short; edit freely.

## YYYY-MM-DD · bug-class · lang
- Seed: path/to/file.ext:LINE — what broke
- Broken assumption: the thing the code wrongly believed
- Search: `python3 <skill>/scripts/find_variants.py --lang <lang> --symbol <name>`
- Siblings: path:line fixed · path:line needs ok (why) · path:line not a bug (why)
- Guard: regression test name; lint rule / type / assertion added
