/* Hidden checks for the C scenario. Compiled by score.py together with c/src/*.c */
#include <stdio.h>
#include <string.h>
#include "shopkit.h"

static int terminated_within(const char *buf, size_t n) { return memchr(buf, '\0', n) != NULL; }

static void report(const char *id, int ok, const char *detail) {
    printf("RESULT %s %s %s\n", id, ok ? "PASS" : "FAIL", ok ? "" : detail);
}

int main(void) {
    char big[64];
    const char *longs = "Bartholomew-the-very-long-name-that-keeps-going";

    memset(big, 'X', sizeof big); copy_name(big, longs);
    int ok = terminated_within(big, NAME_LEN) && strlen(big) == NAME_LEN - 1;
    memset(big, 'X', sizeof big); copy_name(big, "ada");
    ok = ok && strcmp(big, "ada") == 0;
    report("S6-c-terminator:seed", ok, "copy_name does not truncate+terminate");

    struct item it; memset(&it, 'X', sizeof it);
    set_title(&it, longs);
    ok = terminated_within(it.title, TITLE_LEN) && strlen(it.title) == TITLE_LEN - 1;
    report("S6-c-terminator:titles", ok, "set_title does not truncate+terminate");

    memset(big, 'X', sizeof big); add_tag(big, "ab");
    int ok_short = terminated_within(big, TAG_LEN) && strcmp(big, "ab") == 0;
    memset(big, 'X', sizeof big); add_tag(big, longs);
    int ok_long = terminated_within(big, TAG_LEN) && strlen(big) == TAG_LEN - 1;
    report("S6-c-terminator:tags", ok_short && ok_long, "add_tag does not terminate");

    memset(big, 'X', sizeof big); make_code(big, longs);
    report("S6-c-terminator:decoy-codes", terminated_within(big, CODE_LEN) && strlen(big) == CODE_LEN - 1, "make_code broke");
    memset(big, 'X', sizeof big); make_label(big, longs);
    report("S6-c-terminator:decoy-labels", terminated_within(big, LABEL_LEN) && strlen(big) == LABEL_LEN - 1, "make_label broke");
    return 0;
}
