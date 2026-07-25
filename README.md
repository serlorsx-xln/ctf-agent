# Artemis

CTF **agent CLI** — paste a challenge into the TUI, confirm flags interactively, and run a multi-model solver swarm in Docker sandboxes.

Forked from [Veria Labs](https://verialabs.com) [CTF Agent](https://github.com/verialabs/ctf-agent) (BSidesSF 2026: 52/52, 1st place). Artemis keeps the swarm harness and adds the Cursor/Claude/Codex/Gemini TUI product surface.

## Results (upstream Veria)

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

Solves across pwn, rev, crypto, forensics, web, and misc.

## How It Works (TUI)

One flow inside the Artemis TUI:

1. Paste challenge text / path / `@files` → `artemis_load_challenge`
2. If `flags_required` unknown → digits dialog → starts swarm
3. Solvers stream think / bash / tools into the chat; confirm flag candidates in a TUI dialog
4. Summarize how the flag was found

```bash
./chassis/bin/artemis
# or: uv run artemis
```

Connect providers with `/connect` (Cursor, Claude, Codex, Gemini). Host bash/edit are denied — challenge work goes through `artemis_*` tools and the Docker sandbox.

Headless / coordinator multi-challenge mode remains available for batch runs (`uv run artemis swarm --challenge …`). In the TUI, flag confirm is a dialog (not stdin `y/N`). Multiple TUI windows share one daemon; each OpenCode chat is an isolated session slot (see `docs/TUI-PRODUCT-FLOW.md`).

## Quick Start

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

# Launch Artemis TUI (Bun required). Configure providers via /connect — not .env:
#   Cursor / Claude / Codex / Gemini — paste challenge text or a path to solve (swarm + live logs)
uv run artemis
# or: chassis/bin/artemis

# Optional once: warm L0 + common pack caches (faster first solve)
# uv run artemis setup

# Headless swarm (optional CI). Keys from TUI auth.json, or env for automation:
# uv run artemis swarm --challenge ./challenges/my-chal --models cursor/composer-2.5 -v
# alias: uv run ctf-solve …
```

L0 includes common helpers (see `/challenge/TOOLS.txt`). Packs load additively
(`mobile` / `pwn` / `ghidra` / `crypto` / `crypto-tools` / `steg` / `linux` /
`forensics` / `web` / `ml` / `containers`). Exit 137/124 get generic resource
hints; repeated failures ask the agent to change strategy — not a category
playbook.

Coordinator over all local challenges:

```bash
uv run artemis swarm --challenges-dir challenges --max-challenges 10 -v
```

## Coordinator Backends

```bash
# Cursor SDK coordinator (default) — uses CURSOR_API_KEY
uv run artemis swarm --coordinator cursor --coordinator-model composer-2.5 ...

# Claude SDK coordinator
uv run artemis swarm --coordinator claude ...

# Codex coordinator (GPT-5.4 via JSON-RPC)
uv run artemis swarm --coordinator codex ...
```

## Solver Models

Default model lineup (configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| composer-2.5 | Cursor SDK | Default — billed via Cursor API key |
| auto | Cursor SDK | Server-selected Cursor model |
| Claude Opus 4.6 (medium/max) | Claude SDK | Optional — needs `ANTHROPIC_API_KEY` |
| GPT-5.4 / mini / codex | Codex | Optional — needs `OPENAI_API_KEY` + `codex` CLI |
| Gemini 2.5 Flash / Pro | Gemini (`gemini-sdk/`) | Optional — `/connect` Google key, or `GEMINI_API_KEY` / ADC |

Model specs use `provider/model` form, e.g. `cursor/composer-2.5`, `google/gemini-2.5-flash`. After `CORRECT`, the winning solver is asked for a short IDE-style writeup for the recap.

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
| **linux** | linpeas, pspy, ffuf, katana, smbclient, sshpass, impacket, ldap-utils, certipy-ad, bloodhound-python, NetExec (nxc) |
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

**Interactive:** open Artemis TUI and run `/connect` for Cursor, Claude, Codex, and Gemini.
Keys are stored in `~/.local/share/artemis/auth.json`. Do not put API keys in `.env` for day-to-day use — the TUI launcher ignores those secrets so providers stay unchecked until you connect.

**CI / headless swarm:** set `CURSOR_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the environment (or copy `.env.example` → `.env` for Docker/sandbox non-secrets only).

Flags are accepted locally via `submit_flag` (no external scoreboard URL/token required).

## Requirements

- Python 3.14+
- Bun (for Artemis TUI)
- Docker
- Provider credentials via TUI `/connect` (or env for headless swarm)
  - Cursor — [Dashboard → API Keys / Integrations](https://cursor.com/dashboard)
  - Claude — Anthropic API key, `claude setup-token`, or import Claude Code
  - Codex — ChatGPT OAuth or OpenAI API key
  - Gemini — Google AI Studio / Gemini API key
- `codex` CLI (only for Codex solver/coordinator)
- `claude` CLI (only for Claude SDK backend; bundled with claude-agent-sdk)

## Acknowledgements

- Challenge directories: drop `challenge.txt` (paste from the CTF page) plus the
  challenge files. No metadata.yml.