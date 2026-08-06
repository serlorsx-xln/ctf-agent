# Artemis TUI — Product Flow (source of truth)

Locked UX for the OpenCode-based Artemis product shell in this repo
(`chassis/` + `backend/daemon/`). Headless CLI quality reference:
`~/Documents/artemis` (sync fixes into this tree).

**Not** the older Agent-chat-first notes in Documents `FUTURE-CLI.md`.
This product is **Race/solve first**: Solving stays visible; main chat is sendable during a run (send confirms stop+clear). Agent pages lock chat.

## Flow

1. Launch TUI (`uv run artemis` / `chassis/bin/artemis`).
2. Auth Cursor / Claude / Codex / Gemini via `/connect` (or keys). Unavailable providers stay out of the picker.
3. Drop challenge flexibly in one message:
   - **Paste** description / `nc` / **web challenge links** (links stay in text — not downloaded).
   - **One or many folders** and/or **one or many files** (paths → challenge root or materialized workspace + `distfiles/`).
   - Mix paste + paths freely. Missing paths error as `path not found` (never “not a directory” for files).
   Main chat stays **sendable** during Solving; send (or `/restart` / `/new` / `/clear`) with active work → confirm that the solve will be **stopped**, then clear + continue. Agent pages still lock chat (Esc → main).
4. **Dialog** — flags required (default 1). No file config. Skipped after a failed load; stale prior `challenge_dir` must not reopen Flags until a successful load (or a new valid path).
5. **Dialog** — Single vs Swarm (same style as model picker).
6. **Dialog** — models (single pick or multi-select for swarm); only authenticated providers.
7. Daemon `swarm_start` with `flags_required` + `models[]` (requires an existing challenge directory). Live logs via socket → OpenCode-native tool/command rendering.
8. **Swarm UI (multi-model):** main page shows one box per agent (from `swarm_roster`). Always shows **page: main | agent (i/n)**. On **main**: ↑↓ = prompt history / scroll; **Tab** arms swarm select (↑↓ + Enter open). On an **agent page**, ↑↓ select · Enter open; **chat always locked** (Esc → main to type). Global outcomes + **inline flag confirm (y/n)** stay on main. Soft race: `ACCEPTED (n/m)` keeps siblings; `CORRECT` cancels all. **Quota:** SDK usage-limit prints immediately; all-Cursor (or single) emits `[artemis] outcome` and cancels siblings; mixed Cursor+other prints **per-agent only** (soft race continues). While swarm is still running, sibling boxes that have **not started yet** stay `starting…` — they must **not** all paint as `usage limit · failed` just because one agent hit quota (not-started ≠ no-quota). After `swarm_exit`, account quota may mark the grid failed. Blocked-provider / mojibake think streams are suppressed (one short line) so `# Artemis` is not flooded. **Sticky status:** live `Solving · elapsed` (and Esc stop) stays **above the prompt** for single- and multi-agent, and is the **only** live copy — the scrolled tool block keeps a static marker, agent boxes show no elapsed. Elapsed comes from the daemon on reconnect, so a TUI restart mid-run resumes the real time instead of counting from zero. **Solving unlock vs process alive:** terminal outcome unlocks chat/`Solving` but `swarmRunning` stays true until `swarm_exit` so **Esc / `/stop` still work** during teardown. Respawn under the supervisor lock does **not** emit an intermediate `swarm_exit`. **Esc cascade** (bound only when actionable): leave select → back to main → stop while process alive.
9. Flag confirm per candidate → continue until N → **one** summary (no duplicate outcome lines). A pending confirm survives a TUI **reconnect** (grace window before the daemon cancels) and is answered/declined — never silently dropped — so the swarm neither hangs nor loses the operator's answer.
   **Rejecting asks why:** `y` accepts instantly; `n` opens a one-line box (**enter** send · **esc** skip, blank is fine) and the text rides back to the solver as `REJECTED by operator — "…" not confirmed. Operator says: <reason> Continue hunting.` The operator is the only correctness oracle here (no CTFd), so a bare "no" left the solver unable to tell a wrong flag from a wrong technique — it would resubmit variants or decide the checker was broken. While the box is open the `y`/`n` bindings are disabled so the letters reach the input.
   **Attribution:** the accept line names the submitter (`CORRECT — accepted "…" via cursor/grok-4.5`), `FLAG FOUND:` carries the flag **and** `(solved by <agent>)` on one line (never rich-wrapped away from its label), the winning agent's box shows `✓ solved this challenge` (detail line: `solved · flag accepted`), and the run ends with a **How the flag was found** recap in the main chat: winner + model spec + a prose **How:** block from `produce_writeup()` (same session, no tools). Post-CORRECT ai/think chatter is suppressed so the recap is not duplicated. The recap ships as `[artemis] summary …` lines; its first line (`Solved by <agent>[, <agent>]`) is what marks the winning boxes.
10. After summary (or after quota/stop) chat stays on **main**. **New message** while a solve is running → confirm **stop current work** → clear daemon session + swarm UI + **new chat session** → prompt auto-resends once. Same confirm for `/restart`, `/new`, `/clear` when residue or active work exists; `/stop` confirms stop only (no clear).
11. Performance: happy path uses **daemon NDJSON only** (bridge spawn is fallback if socket is down).

## Architecture

```
TUI window A (sessionID=A) ─┐
TUI window B (sessionID=B) ─┼─ NDJSON unix socket ─► artemis-daemon
CLI / headless (_default)  ─┘                              │
                                                    SessionSlot A|B|_default
                                                    (swarm + flags + usage + logs)
                                                              │
                                                    ChallengeSwarm + Docker L0/packs
                                                    (label ctf-agent.session-id)
```

One daemon supports **multiple concurrent TUI windows**. Each OpenCode chat
`sessionID` maps to a daemon session slot under `~/.cache/artemis/sessions/<sid>/`
(`session.json`, `swarm.pid`, swarm logs). Missing session → `_default` (CLI /
tests). No hard concurrent-session cap — how many live swarms you can run
depends on the machine. Auth and pack cache stay shared. Closing a TUI does
**not** auto-stop that session’s swarm (reconnect-friendly); `/exit`,
`swarm_stop`, or daemon teardown still stop. Batch multi-challenge runs stay
on the Veria-style CLI coordinator (`--challenges-dir` / `--max-challenges`),
not multi-window TUI.

Key modules:

| Area | Path |
|------|------|
| Solve gate | `chassis/packages/tui/src/util/artemis-solve-flow.ts` |
| Mode dialog | `chassis/packages/tui/src/component/dialog-solve-mode.tsx` |
| Swarm models (scroll + search + provider groups) | `chassis/packages/tui/src/component/dialog-models-swarm.tsx` |
| Swarm agent partition | `chassis/packages/tui/src/util/artemis-swarm-agents.ts` |
| Shared clear/stop checks | `chassis/packages/tui/src/util/artemis-solve-state.ts` |
| Per-runner log attribution | `backend/log_context.py` |
| Solve recap / winner credit | `ChallengeSwarm.solve_writeup` + `backend/cli.py:print_swarm_outcome` |
| Keeping the agent on task | `backend/continue_prompt.py` (pivot on zero progress) + Veria-shaped `backend/prompts.py` |
| Swarm footer (↑↓ enter / esc stop) | `chassis/packages/tui/src/component/swarm-agent-footer.tsx` |
| Inline flag confirm | `chassis/packages/tui/src/component/flag-confirm-bar.tsx` |
| Daemon client | `chassis/packages/tui/src/artemis/client.ts` |
| Plugin tools | `chassis/artemis/plugin.ts` |
| Handlers | `backend/daemon/handlers.py` |
| Flexible load | `backend/challenge.py` (`resolve_load_target`) + `backend/shell/cursor_llm_stub.py` |

## Definition of done

- No file edits required for flag count / single|swarm / models.
- Chat sendable on main during Solving; send with active work = confirm stop + clear (chat + sidebar + swarm) then new session with auto-resend. Agent pages lock chat; solve-flow dialogs still block. `/restart` / `/new` / `/clear` same confirm when residue/active; `/stop` confirms stop only. All four paths share one residue/active check (`util/artemis-solve-state.ts`) and never dismiss the open solve gate.
- During model gate: ignore reconnect replay of old swarm logs; clear stale agents before start; clear `solveFlowBusy` before `startSwarm` so boot/roster are not dropped.
- Daemon-first ops; pytest green; CLI parity for vision / Cursor errors / no Bedrock·Azure race fallback.
- `artemis_swarm` must not bypass the TUI gate (alias of `artemis_ask_flags` only).
- Model picker lists only `/connect`-authenticated providers among cursor / anthropic / openai / google.
- After CORRECT, daemon rehydrates `accepted_flags` so `flowCompleted` / sidebar / restart-confirm work.
- A finished run names its winner: accept line, `FLAG FOUND:` (flag on the same line as its label), the agent box, and the `How the flag was found` recap all agree on one label — the one on the grid.
- Parallel solvers tag their own sandbox/boot logs (`backend/log_context.py`), so two containers booting never read as one duplicated block.
- **Agents are steered lightly, not tutored into a category.** The system prompt follows Veria's skeleton (header + files + full tutoring list + “cover maximum surface area”), with two Artemis-only changes: a human confirms flags, and the pyghidra block appears only when a decompilable file is attached. A turn that ends with **zero accepted flags** is told to change surface (`backend/continue_prompt.py`); partial accepts and infra recoveries still resume. There is no scope-guard bash filter and no category-based hint withholding.
- Swarm model picker is usable at any list length: the options live in a `scrollbox` sized to the terminal with the cursor scrolled into view, grouped under provider headings, and filtered by a focused search input. `tab` toggles; `space` also toggles but only while the query is empty, because model names contain spaces.
- TUI connects to daemon on boot (and tool-card fallback) so solve-flow dialogs always open after load.
- Single dark theme (`opencode`); no `/themes` or theme mode switching.
- Multi-model swarm: N agent boxes on main, focusable agent pages, inline y/n flag confirm on main, soft-race semantics unchanged.
- Stop (`esc` after leave-select / back-to-main, or `/stop`) while the swarm **process** is alive (`swarmRunning`) — even after Solving unlocked on quota/CORRECT; never during setup dialogs. Esc is unbound when it would be a no-op (so `session.interrupt` still works).
- **Exit cleanup:** `/exit`, Ctrl+C, TUI teardown, or daemon SIGTERM/SIGINT must `swarm_stop` and reclaim orphan `ctf-sandbox-*` containers — the swarm must not keep running after the operator leaves.
- While swarm process is alive (`swarmRunning`), agent grid uses process-alive (not Solving-UI) for not-started ≠ quota; Solving spinner may unlock earlier on terminal outcome.
- Load accepts folder(s), file(s), and paste (web links in paste only); failed reload does not reopen Flags for a prior challenge.

## Out of scope (for now)

- Auto multi-model without picker.
- Cursor SDK host-tool deny (API limitation).
- Richer `artemis setup` CI bake automation — local Colima detect, digest stale rebuild, and cross-process bake lock already ship.
- Auto-kill slow agents after partial `ACCEPTED` (operator may stop via esc, `/stop`, or tool `artemis_stop_swarm`).
- Historical Solve cards snapshotting (all cards currently share the live `swarmEvents` buffer).
- HTTP-fetching challenge URLs as a load target (links are paste/writeup targets only).
