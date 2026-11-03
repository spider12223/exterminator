#ifndef SHOPKIT_H
#define SHOPKIT_H

#include <stddef.h>

#define NAME_LEN 16
#define TITLE_LEN 24
#define TAG_LEN 8
#define CODE_LEN 12
#define LABEL_LEN 20

struct item {
    char title[TITLE_LEN];
    int price_cents;
};

/* All copies truncate to (size - 1) characters and always NUL-terminate. */
void copy_name(char dst[NAME_LEN], const char *src);
void set_title(struct item *it, const char *title);
void add_tag(char tag[TAG_LEN], const char *src);
void make_code(char dst[CODE_LEN], const char *src);
void make_label(char dst[LABEL_LEN], const char *src);

#endif
