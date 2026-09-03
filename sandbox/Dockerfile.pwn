# Pwn runtime + donor (extends L0 core — Ubuntu 24.04, shared with mobile/crypto).
#
# Build order (context = sandbox/):
#   docker build -f Dockerfile.core -t ctf-sandbox-core .   # run from sandbox/
#   docker build -f Dockerfile.pwn -t ctf-sandbox-pwn .
#
# When ``pwn`` is prefetched, sandboxes start from this image instead of core so
# apt/pip/gem bootstrap becomes a no-op (qemu wrappers still written at ensure time).

FROM ctf-sandbox-core

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    nmap nasm patch \
    patchelf \
    qemu-user-static binfmt-support \
    libc6-amd64-cross libc6-i386-cross \
    binutils-x86-64-linux-gnu \
    gdb-multiarch \
    radare2 \
    ruby ruby-dev \
    libc6-dbg \
    && rm -rf /var/lib/apt/lists/*

# Core already ships pwntools + pyelftools; add the heavy RE/pwn stack once here.
RUN pip3 install --no-cache-dir --break-system-packages \
    ROPgadget capstone unicorn keystone-engine angr

RUN gem install one_gadget seccomp-tools --no-document

# Host pack cache bind-mounts these paths from this donor image.
RUN curl -fsSL https://raw.githubusercontent.com/hugsy/gef/main/gef.py -o /root/.gdbinit-gef.py \
    && printf 'source /root/.gdbinit-gef.py\n' > /root/.gdbinit

COPY sandbox-tools-pwn.txt /tools.txt

WORKDIR /challenge
CMD ["sleep", "infinity"]
