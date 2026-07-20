# Lightweight SageMath sandbox for crypto challenges.
# docker build -f sandbox/Dockerfile.sage -t ctf-sandbox .

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TERM=xterm

RUN apt-get update && apt-get install -y \
    sagemath \
    python3 python3-pip python3-dev \
    gcc g++ make \
    libgmp-dev libmpfr-dev libfplll-dev \
    curl wget netcat-openbsd \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    pycryptodome \
    sympy \
    numpy \
    pwntools

WORKDIR /challenge
CMD ["sleep", "infinity"]
