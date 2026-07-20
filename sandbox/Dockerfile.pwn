# Pwn-focused sandbox.
#
# Native (fast on Apple Silicon / Colima aarch64) — use qemu for x86_64 bins:
#   docker build -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .
#
# Or force amd64 (slower via qemu, but native for typical CTF ELFs):
#   docker build --platform linux/amd64 -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .
#
# Run:
#   uv run ctf-solve --image ctf-sandbox-pwn --challenge ./challenges/... ...

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TERM=xterm
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git file xxd bsdmainutils \
    netcat-openbsd socat nmap \
    gcc g++ make cmake nasm patch pkg-config \
    gdb gdbserver strace ltrace \
    binutils patchelf \
    qemu-user-static binfmt-support \
    # Guest glibc for qemu-user on aarch64 hosts (x86_64 / i386 CTF bins).
    libc6-amd64-cross libc6-i386-cross \
    python3 python3-pip python3-dev python3-venv \
    ruby ruby-dev \
    libssl-dev libffi-dev \
    libc6-dbg \
    && rm -rf /var/lib/apt/lists/*

# radare2 from upstream (not in jammy arm64 apt)
RUN git clone --depth=1 https://github.com/radareorg/radare2 /tmp/r2 \
    && cd /tmp/r2 && ./sys/install.sh --install \
    && ldconfig \
    && rm -rf /tmp/r2

# Python pwn stack
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel \
    && pip3 install --no-cache-dir \
        pwntools \
        ROPgadget \
        capstone \
        unicorn \
        keystone-engine \
        requests \
        pyelftools

# GEF for gdb
RUN curl -fsSL https://raw.githubusercontent.com/hugsy/gef/main/gef.py -o /root/.gdbinit-gef.py \
    && printf 'source /root/.gdbinit-gef.py\n' > /root/.gdbinit

# Ruby pwn helpers
RUN gem install one_gadget seccomp-tools --no-document

# qemu-user needs -L <cross-prefix> when the guest glibc lives under
# /usr/x86_64-linux-gnu (typical aarch64 host). Wrappers no-op the -L when
# that prefix is absent (native amd64). Never overwrite amd64's /lib64 ld.
RUN set -e; \
    case "$(uname -m)" in aarch64|arm64) \
      mkdir -p /lib64; \
      if [ ! -e /lib64/ld-linux-x86-64.so.2 ]; then \
        ln -sfn /usr/x86_64-linux-gnu/lib/ld-linux-x86-64.so.2 \
          /lib64/ld-linux-x86-64.so.2; \
      fi ;; \
    esac; \
    printf '%s\n' \
        '#!/bin/bash' \
        'PREFIX="${QEMU_LD_PREFIX:-/usr/x86_64-linux-gnu}"' \
        'for a in "$@"; do case "$a" in -L) exec /usr/bin/qemu-x86_64-static "$@";; esac; done' \
        'if [ -d "$PREFIX/lib" ]; then exec /usr/bin/qemu-x86_64-static -L "$PREFIX" "$@"; fi' \
        'exec /usr/bin/qemu-x86_64-static "$@"' \
        > /usr/local/bin/qemu-x86_64-static; \
    printf '%s\n' \
        '#!/bin/bash' \
        'PREFIX="${QEMU_LD_PREFIX:-/usr/i686-linux-gnu}"' \
        'for a in "$@"; do case "$a" in -L) exec /usr/bin/qemu-i386-static "$@";; esac; done' \
        'if [ -d "$PREFIX/lib" ]; then exec /usr/bin/qemu-i386-static -L "$PREFIX" "$@"; fi' \
        'exec /usr/bin/qemu-i386-static "$@"' \
        > /usr/local/bin/qemu-i386-static; \
    printf '%s\n' '#!/bin/bash' 'exec qemu-x86_64-static "$@"' > /usr/local/bin/q64; \
    printf '%s\n' '#!/bin/bash' 'exec qemu-i386-static "$@"' > /usr/local/bin/q32; \
    chmod +x /usr/local/bin/qemu-x86_64-static /usr/local/bin/qemu-i386-static \
        /usr/local/bin/q64 /usr/local/bin/q32

COPY sandbox/sandbox-tools-pwn.txt /tools.txt

WORKDIR /challenge
CMD ["sleep", "infinity"]
