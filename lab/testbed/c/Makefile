CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -g -Iinclude
SRC := $(wildcard src/*.c)
TESTS := $(wildcard tests/test_*.c)
BINS := $(patsubst tests/%.c,build/%,$(TESTS))

.PHONY: test clean
test: $(BINS)
	@for t in $(BINS); do ./$$t || exit 1; done

build/%: tests/%.c $(SRC) include/shopkit.h
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< $(SRC)

clean:
	rm -rf build
