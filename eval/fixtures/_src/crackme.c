#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc != 2) {
        puts("usage: crackme <password>");
        return 1;
    }
    if (strcmp(argv[1], "s3cret") == 0) {
        puts("flag{owned_rev_rodata}");
        return 0;
    }
    puts("nope");
    return 1;
}
