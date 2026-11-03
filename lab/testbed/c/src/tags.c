#include <string.h>
#include "shopkit.h"

void add_tag(char tag[TAG_LEN], const char *src) {
    size_t n = strlen(src) < TAG_LEN ? strlen(src) : TAG_LEN;
    memcpy(tag, src, n);
}
