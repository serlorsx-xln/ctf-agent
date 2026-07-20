# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages local challenges under `challenges/` while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to finish the required flag(s) wins.

```
                        +-----------------+
                        | challenges/     |
                        | (local dirs)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Local poller    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Cursor/Claude) |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  composer-2.5   | |  composer-2.5  | |     ...        |
     |  (Cursor SDK)   | |  (Cursor SDK)  | |                |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools. Flags are accepted **locally** (plausible non-decoy strings) — there is no external scoreboard dependency. By default one accepted flag ends the run (`CORRECT`). Multi-flag challenges set `flags_required: N` in `challenge.txt` (nth distinct accept → `CORRECT`).

## Quick Start (Cursor API key)

```bash
# Install
uv sync

# Build L0 sandbox (+ optional pack donors)
docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core .
docker build -f sandbox/Dockerfile.mobile -t ctf-sandbox-mobile .
docker build -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .
# SageMath donor for .sage challenges (first build is large / slow):
docker build -f sandbox/Dockerfile.crypto -t ctf-sandbox-crypto .
# Optional donors (loaded on demand; multi-stage — toolchain not kept in final image):
# docker build -f sandbox/Dockerfile.crypto-tools -t ctf-sandbox-crypto-tools .
# docker build -f sandbox/Dockerfile.ghidra -t ctf-sandbox-ghidra .   # PyGhidra / analyzeHeadless
# docker build -f sandbox/Dockerfile.steg -t ctf-sandbox-steg .
# docker build -f sandbox/Dockerfile.linux -t ctf-sandbox-linux .

# Configure credentials
cp .env.example .env
# Set CURSOR_API_KEY from https://cursor.com/dashboard/integrations

# Drop a challenge folder, then solve:
#   challenges/my-chal/challenge.txt   ← paste from the CTF page
#   challenges/my-chal/...files...     ← attachments (or under distfiles/)
uv run ctf-solve --challenge ./challenges/my-chal --models cursor/composer-2.5 -v

# Harder challenges (same key / other backends):
# uv run ctf-solve --challenge ./challenges/my-chal --models cursor/claude-4-sonnet -v
```

L0 includes common helpers (see `/challenge/TOOLS.txt`). Packs load additively
(`mobile` / `pwn` / `ghidra` / `crypto` / `crypto-tools` / `steg` / `linux` /
`forensics` / `web` / `ml` / `containers`). Exit 137/124 get generic resource
hints; repeated failures ask the agent to change strategy — not a category
playbook.

Coordinator over all local challenges:

```bash
uv run ctf-solve --challenges-dir challenges --max-challenges 10 -v
```

## Coordinator Backends

```bash
# Cursor SDK coordinator (default) — uses CURSOR_API_KEY
uv run ctf-solve --coordinator cursor --coordinator-model composer-2.5 ...

# Claude SDK coordinator
uv run ctf-solve --coordinator claude ...

# Codex coordinator (GPT-5.4 via JSON-RPC)
uv run ctf-solve --coordinator codex ...
```

## Solver Models

Default model lineup (configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| composer-2.5 | Cursor SDK | Default — billed via Cursor API key |
| auto | Cursor SDK | Server-selected Cursor model |
| Claude Opus 4.6 (medium/max) | Claude SDK | Optional — needs `ANTHROPIC_API_KEY` |
| GPT-5.4 / mini / codex | Codex | Optional — needs `OPENAI_API_KEY` + `codex` CLI |

Model specs use `provider/model` form, e.g. `cursor/composer-2.5` or `cursor/auto`.

## Sandbox Tooling

Default runtime is **L0** `ctf-sandbox-core` plus **additive packs** loaded on
demand. Prefer core + packs; do not use a monolithic all-in-one image.
Donors for `linux` / `steg` / `crypto-tools` are multi-stage (build toolchain
discarded from the final image).

| Layer | Tools (representative) |
|-------|------------------------|
| **L0 core** | python3, pwntools, z3, gdb, binutils, curl, socat, gf128-roots |
| **pwn** | qemu-user (+ guest libc on aarch64), GEF, ROPgadget, one_gadget, patchelf, angr, r2 |
| **ghidra** | Ghidra + pyghidra / analyzeHeadless (prefetched with ELF / on `import pyghidra`) |
| **crypto** | SageMath, pycryptodome (in Sage), galois |
| **crypto-tools** | flatter, cado-nfs, RsaCtfTool, fpylll, gmpy2 |
| **mobile** | jadx, apktool, blutter, frida-tools, androguard |
| **steg** | steghide, stegseek, zsteg, exiftool, tesseract |
| **linux** | linpeas, pspy, ffuf, smbclient, sshpass, impacket, ldap-utils, certipy-ad, bloodhound-python, NetExec (nxc) |
| **forensics** | sleuthkit, binwalk, volatility3, tshark, scapy |
| **web** | nmap, sqlmap, flask, PyJWT |
| **ml / containers** | torch/keras, podman — as needed |

Heavy packs raise the container memory floor automatically (e.g. crypto ≥12g).
Host pack cache defaults to 25 GiB with LRU eviction (`CTF_PACK_CACHE_MAX_GB`).
After rebuilding donors: `bash scripts/prune_docker.sh`.

On **macOS**, lab VPNs (e.g. HackTheBox) work automatically (`CTF_HOST_PROXY=auto`):
use direct container routing when the lab is reachable (TCP open **or** connection
refused); fall back to host SOCKS + proxychains when it is not. Challenge text
IPs/ports are probed automatically so custom-only services still calibrate.
Agent `nmap` is forced to TCP connect scan (`-Pn -sT`); full-port sweeps are
kept intact (timeout auto-extends). No manual Colima routes or `pf` NAT.

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
CURSOR_API_KEY=cursor_...
# Optional alternate backends:
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

All settings can also be passed as environment variables or CLI flags.
Flags are accepted locally via `submit_flag` (no external scoreboard URL/token required).

## Requirements

- Python 3.14+
- Docker
- `CURSOR_API_KEY` (primary) — from [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations)
- Optional: Anthropic / OpenAI / Google keys for non-Cursor backends
- `codex` CLI (only for Codex solver/coordinator)
- `claude` CLI (only for Claude SDK backend; bundled with claude-agent-sdk)

## Acknowledgements

- Challenge directories: drop `challenge.txt` (paste from the CTF page) plus the
  challenge files. No metadata.yml.