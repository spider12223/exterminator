#include <stdio.h>
#include "shopkit.h"

void make_code(char dst[CODE_LEN], const char *src) {
    snprintf(dst, CODE_LEN, "%s", src);
}
