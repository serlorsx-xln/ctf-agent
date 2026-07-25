# Artemis Handoff

**Updated:** 2026-07-25  
**Repo:** `/Users/serlorsx/Downloads/ctf`  
**Branch:** `cursor-backend` (mostly uncommitted; do **not** commit unless user asks)

**Product flow (source of truth):** [`docs/TUI-PRODUCT-FLOW.md`](docs/TUI-PRODUCT-FLOW.md)

Prior chat transcript (optional deep dive):  
`/Users/serlorsx/.cursor/projects/Users-serlorsx-Downloads-ctf/agent-transcripts/a87e9401-c0f0-42ff-9ce4-93d68c3ebce3/`

---

## Product

Artemis = CTF agent on OpenCode TUI (`chassis/`) + Python backend (`backend/`) + daemon socket.

Flow: auth (`/connect`: Cursor / Claude / Codex / Gemini) → drop challenge → **dialogs** (flags → single/swarm → models) → swarm via daemon → flag confirm → IDE-style writeup recap → chat unlock / confirm-restart.

**Not** product UX: Ask / Agent / Race mode switcher from older FUTURE-CLI notes.

Stable headless CLI reference (quality parity): `~/Documents/artemis`.

---

## Hard rules from user

1. Do not claim 100% confidence / do not lie.
2. Token/cost UI = **provider SDK reported only**. No client estimates, no local price tables, no guessed tokens.
3. No GitHub / foreign brand splash on screen (small OpenCode credit OK).
4. Logo = FIGlet **Bloody** `ARTEMIS` in `chassis/packages/tui/src/logo.ts` (also used by `logo.tsx`, `presentation.ts`, `cli/ui.ts`). Compact `go` still for splash/bg-pulse.

---

## Done (already fixed in tree)

| Issue | Root / fix |
|---|---|
| Stale flags after New session | Per-session `sessions/<sid>/session.json`; `clear_session` scoped to OpenCode sessionID; plugin `session.created` → clear that slot; swarm resets accepted flags / usage |
| Duplicate Confirm / CORRECT lines | Print went to stdout+stderr while bridge merges stderr→stdout; `_emit_line` stdout once; `dedupeEvents` in `artemis-live-log.ts` |
| Mid-token spaces in flags (`fl ag{…}`) | `_join_stream` / `joinThink` inserted spaces between alnum chunks; concatenate deltas as-is; wrapMode `char` for flag/command/outcome |
| Orphan text TUI crash (`" "` under `<box>`) | Bare space / array siblings in logo JSX; flatten glyph cells; wrap gaps in `<text>`; cleaned WorkspaceLabel / footers |
| Brands | Removed GitHub issue URL from crash UI; theme `github`→display `mono`; docs open local README; soft OpenCode credit |
| Sidebar Context fake/stub | Prefer daemon `usage_update` push (`CostTracker` → daemon); debug file `sessions/<sid>/usage.json` (or legacy root `usage.json` for `_default`); **no stub fallback** → show `—` until real usage |
| Cursor mid-run usage wiring | Listen `SDKUsageMessage` + `publish_with_pending`; commit on turn end via `record_tokens`. Claude/Codex already record mid-run |
| Prompt footer `72 (0%)` stub | `chassis/packages/tui/src/component/prompt/index.tsx` — `usage` memo returns empty when `ARTEMIS===1` (sidebar owns real usage) |
| Sidebar bottom path stuck on old challenge | Footer / challenge panel read `daemon.sessionState` push (not file polling) |
| Gemini in `/connect` | TUI `ARTEMIS_CHAT_PROVIDERS` includes `google` → `gemini-sdk/`; backend `GeminiSolver` |
| Post-CORRECT writeup | `backend/writeup.py` + solver `produce_writeup` → narrative How: recap (tool trail fallback) |

---

## Remaining work

1. Live verify multi-window TUI + Cursor mid-run usage / writeup in a real session (unit tests cover daemon multi-slot wiring).
2. Do **not** commit or push unless the user asks.

Legacy aliases kept intentionally: ``artemis race`` / ``normalize_race_spec`` / ``ctf-solve`` → swarm.

---

## Key paths

| Area | Path |
|---|---|
| Logo | `chassis/packages/tui/src/logo.ts`, `component/logo.tsx`, `util/presentation.ts`, `opencode/src/cli/ui.ts` |
| Live log / dedupe / join | `chassis/packages/tui/src/util/artemis-live-log.ts`, `backend/agents/live_log.py` |
| Flags emit | `backend/flags.py` (`_emit_line`) |
| Usage publish | `backend/cost_tracker.py`, `backend/agents/cursor_solver.py` |
| Writeup recap | `backend/writeup.py`, `ChallengeSwarm.solve_writeup`, `cli.print_swarm_outcome` |
| Gemini | `backend/agents/gemini_solver.py`, `chassis/.../artemis-models.ts` |
| Multi-session daemon | `backend/daemon/{state,supervisor,server,handlers}.py`, `sandbox_session.py` |
| Clear session | `backend/shell/sandbox_session.py`, `chassis/artemis/plugin.ts` |

---

## Tests related to fixes

- `tests/test_live_log.py`, `tests/test_cost_tracker.py`, `tests/test_flags.py`
- `tests/test_writeup.py`, `tests/test_solve_attribution.py`
- `tests/test_daemon_multi_session.py`, `tests/test_daemon_state_hydrate.py`, `tests/test_daemon_supervisor.py`
- TUI: `artemis-models.test.ts`, `session-filter.test.ts`, provider-options under `ARTEMIS=1`

---

## Git note

Large uncommitted set. User has not asked to commit. Ask before committing or pushing.

---

## GLM via pool.nack.cafe (from chat history 2026-07-20)

Claude Agent SDK pointed at Anthropic-compatible gateway; model id `glm-5.2`.

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
unset ANTHROPIC_API_KEY   # important: empty, else it overrides AUTH_TOKEN
export ANTHROPIC_BASE_URL="https://pool.nack.cafe"
export ANTHROPIC_AUTH_TOKEN="sk-glm-<REDACTED>"
export ANTHROPIC_DEFAULT_SONNET_MODEL="glm-5.2"
export ANTHROPIC_DEFAULT_OPUS_MODEL="glm-5.2"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-5.2"

cd /Users/serlorsx/Downloads/ctf
uv run ctf-solve \
  --challenge ./challenges/pingpong \
  --models claude-sdk/glm-5.2 \
  -v
```

Rotate any token that was previously pasted into this file — treat it as compromised.
