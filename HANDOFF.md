# Artemis Handoff

**Updated:** 2026-07-26  
**Repo:** `/Users/serlorsx/Downloads/ctf`  
**Branch:** `cursor-backend`

**Product flow (source of truth):** [`docs/TUI-PRODUCT-FLOW.md`](docs/TUI-PRODUCT-FLOW.md)

---

## Product

Artemis = CTF agent on OpenCode TUI (`chassis/`) + Python backend (`backend/`) + daemon socket.

Flow: auth (`/connect`) → drop challenge → dialogs (flags → single/swarm → models) → swarm via daemon → flag confirm → prose writeup recap → chat unlock.

---

## Recent backend/TUI state (2026-07-26)

| Area | Status |
|------|--------|
| Pwn runtime | `ctf-sandbox-pwn` baked; bootstrap ~1s |
| Writeup recap | Prose-only via `produce_writeup()` (no command trail) |
| Post-CORRECT TUI | `dropPostSolveAgentChatter` suppresses duplicate ai/think |
| Action log | Removed unused per-tool `_action_log` chain |
| Tests | 359+ pytest; TUI live-log tests green |

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

Legacy aliases kept intentionally: `artemis race` / `normalize_race_spec` / `ctf-solve` → swarm.

---

## Hard rules from user

1. Do not claim 100% confidence / do not lie.
2. Token/cost UI = provider SDK reported only.
3. Do not commit or push unless the user asks.
