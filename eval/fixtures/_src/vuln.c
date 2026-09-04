#include <stdio.h>
#include <string.h>

struct {
    char buf[16];
    int auth;
} g;

int main(void) {
    memset(&g, 0, sizeof g);
    setvbuf(stdout, NULL, _IONBF, 0);
    puts("name?");
    if (!fgets(g.buf, 64, stdin)) {
        return 1;
    }
    if (g.auth) {
        puts("flag{owned_pwn_auth}");
    } else {
        puts("nope");
    }
    return 0;
}
