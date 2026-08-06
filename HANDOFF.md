# Artemis Handoff

**Updated:** 2026-08-06  
**Repo:** `/Volumes/UNITEK/project/artemis/tui` (or clone path)  
**Branch:** `cursor-backend`

**Product flow (source of truth):** [`docs/TUI-PRODUCT-FLOW.md`](docs/TUI-PRODUCT-FLOW.md)

---

## Product

Artemis = CTF agent on OpenCode TUI (`chassis/`) + Python backend (`backend/`) + daemon (Unix socket on macOS/Linux, TCP localhost on Windows).

Flow: auth (`/connect`) → drop challenge → dialogs (flags → single/swarm → models) → swarm via daemon → flag confirm → prose writeup recap → chat unlock.

---

## Recent backend/TUI state (2026-08-06)

| Area | Status |
|------|--------|
| Cross-platform daemon | `backend/daemon/transport.py` — Unix socket + TCP `daemon.port` |
| Windows launch | `chassis/bin/artemis.cmd`, `artemis.ps1` |
| Pwn / mobile runtime | L0 images `ctf-sandbox-pwn`, `ctf-sandbox-mobile` |
| Writeup recap | Prose-only via `produce_writeup()` (no command trail) |
| Post-CORRECT TUI | `dropPostSolveAgentChatter` suppresses duplicate ai/think |
| Tests | 359 pytest; daemon transport + dialog tests green |

Latest commit on branch: see `git log -1`.

---

## Key paths

| Area | Path |
|------|------|
| Writeup recap | `backend/writeup.py`, `ChallengeSwarm.solve_writeup` |
| Early prose snapshot | `backend/action_log.py` (`notes_from_prose`) |
| Live log / dedupe | `chassis/packages/tui/src/util/artemis-live-log.ts` |
| Sandbox / packs | `backend/tool_router.py`, `backend/sandbox/` |
| Multi-session daemon | `backend/daemon/` |
| Docker bind paths | `backend/platform_paths.py` |

Legacy aliases kept intentionally: `artemis race` / `normalize_race_spec` / `ctf-solve` → swarm. `ctf-msg` → one-shot daemon RPC.

---

## Hard rules from user

1. Do not claim 100% confidence / do not lie.
2. Token/cost UI = provider SDK reported only.
3. Do not commit or push unless the user asks.
4. User handles video assets separately.
