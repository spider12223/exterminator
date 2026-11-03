#!/usr/bin/env sh
# Runs every test suite whose toolchain is installed. Exit code is non-zero if any suite fails.
status=0
root=$(cd "$(dirname "$0")" && pwd)
if command -v node >/dev/null; then (cd "$root/js" && node --test) || status=1; fi
if command -v python3 >/dev/null; then (cd "$root/py" && python3 -m unittest discover -s tests -t .) || status=1; fi
if command -v luau >/dev/null; then (cd "$root" && luau luau/tests/run.luau) || status=1; fi
if command -v make >/dev/null && command -v cc >/dev/null; then (cd "$root/c" && make -s test) || status=1; fi
exit $status
