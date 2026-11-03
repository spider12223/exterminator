#include <string.h>
#include "shopkit.h"

void set_title(struct item *it, const char *title) {
    strncpy(it->title, title, sizeof it->title);
}
