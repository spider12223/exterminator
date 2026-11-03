# C and C++

`FV` = `python3 <skill>/scripts/find_variants.py --lang c` (or `--lang cpp`).

First move for any crash or garbage output: rebuild the repro with sanitizers. They turn "random" memory bugs into an exact report with the line that broke the rules.

```bash
cc  -g -O1 -fsanitize=address,undefined -fno-omit-frame-pointer ...   # gcc/clang
c++ -g -O1 -fsanitize=address,undefined -fno-omit-frame-pointer ...
# threads: -fsanitize=thread (not combinable with address)
```
MSVC: `/fsanitize=address`. Valgrind is the fallback where sanitizers aren't available.

## Contents
- [Hunting recipes by class](#hunting-recipes-by-class)
- [C++-specific](#c-specific)
- [Class guards](#class-guards)

## Hunting recipes by class

**memory-safety: missing terminator / overflow**
```bash
FV --ast 'strncpy($D, $S, $N)' --regex '\bstrncpy\s*\('
FV --regex '\b(strcpy|strcat|sprintf|gets)\s*\('                  # unbounded
FV --ast 'memcpy($D, $S, strlen($S))' --regex 'memcpy\([^;]*strlen\('   # copies without the NUL
FV --regex 'sizeof\s*\(\s*\w+\s*\)\s*/\s*sizeof' --regex 'sizeof\s*\(\s*\w+\s*\)\s*\)' # sizeof(pointer) mistakes
```
`strncpy(dst, src, n)` does **not** terminate when `strlen(src) >= n`. Verify each call: is `dst[n-1] = '\0'` set afterwards, or is `n` smaller than the buffer?

**off-by-one**
```bash
FV --regex 'for\s*\([^;]*;\s*\w+\s*<=\s*\w+' --regex '\[\s*\w+\s*\+\s*1\s*\]' --regex 'malloc\(\s*strlen\([^)]*\)\s*\)'
```
`malloc(strlen(s))` forgets the terminator; `<=` loops over `[0, n]` write one past the end.

**unchecked-null / ignored return values**
```bash
FV --regex '=\s*(malloc|calloc|realloc|fopen|strdup)\s*\(' ; FV --regex '^\s*(fread|fwrite|write|read|fclose|snprintf)\s*\('
FV --regex '\w+\s*=\s*realloc\(\s*\w+\s*,'          # p = realloc(p, n) leaks p on failure
```

**use-after-free / double free / dangling**
```bash
FV --regex '\bfree\s*\(' ; FV --regex 'return\s+&\w+\s*;'   # returning address of a local
```
After each `free(p)`: is `p` used later, freed again on another path, or still referenced elsewhere?

**integer overflow / signedness**
```bash
FV --regex 'malloc\([^)]*\*[^)]*\)'                  # n * size can overflow: use calloc or check
FV --regex '\(int\)\s*\w*len\w*' --regex 'int\s+\w+\s*=\s*\w+\.size\(\)'
```

**swallowed-error**: ignored return codes, `catch (...) {}`, `errno` never checked.

## C++-specific

- Iterator/reference invalidation: holding iterators, pointers, or `string_view`s into a `vector`/`string` across `push_back`/`insert`/`erase`.
- Dangling `std::string_view` / `std::span` to a temporary.
- Missing `virtual` destructor in a base class deleted through a base pointer.
- Rule of 3/5: raw owning pointers in a class with a defaulted copy constructor → double free.
- Lambdas capturing by reference (`[&]`) that outlive the scope (stored callbacks, threads).
- `std::move` then reuse of the moved-from object.
```bash
FV --lang cpp --regex '\[&\]' --regex 'string_view' --regex 'std::move\('
```

## Class guards

| Class | Guard |
|---|---|
| memory-safety | ASan+UBSan in the test build; `snprintf`/bounded copy helper that always terminates; `std::string`/`std::vector`/`std::span` in C++ |
| ignored returns | `__attribute__((warn_unused_result))` / `[[nodiscard]]` on functions whose result matters |
| warnings | `-Wall -Wextra -Werror` (plus `-Wformat=2 -Wconversion` where practical); never `-w` |
| static analysis | clang-tidy `bugprone-*`, `cert-*`, `clang-analyzer-*`; cppcheck |
| duplicated-logic-drift | one helper for buffer copies/size math; ban raw `strncpy` via a lint rule or `#pragma GCC poison strcpy strcat sprintf` |
