#include <string.h>
#include "shopkit.h"

void copy_name(char dst[NAME_LEN], const char *src) {
    strncpy(dst, src, NAME_LEN);
}
