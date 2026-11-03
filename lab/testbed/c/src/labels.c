#include <string.h>
#include "shopkit.h"

void make_label(char dst[LABEL_LEN], const char *src) {
    strncpy(dst, src, LABEL_LEN - 1);
    dst[LABEL_LEN - 1] = '\0';
}
