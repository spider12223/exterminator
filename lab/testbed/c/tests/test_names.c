#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "shopkit.h"

int main(void) {
    char buf[NAME_LEN + 8];

    memset(buf, 'X', sizeof buf);
    copy_name(buf, "ada");
    assert(strcmp(buf, "ada") == 0);

    memset(buf, 'X', sizeof buf);
    copy_name(buf, "Bartholomew-the-very-long-name");
    assert(memchr(buf, '\0', NAME_LEN) != NULL && "long names must be truncated and terminated");
    assert(strlen(buf) == NAME_LEN - 1);

    puts("test_names: ok");
    return 0;
}
