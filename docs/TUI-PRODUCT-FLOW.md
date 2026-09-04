# Artemis TUI — Product Flow (source of truth)

Locked UX for the OpenCode-based Artemis product shell in this repo
(`chassis/` + `backend/daemon/`). Headless CLI quality reference:
`~/Documents/artemis` (sync fixes into this tree).

**Not** the older Agent-chat-first notes in Documents `FUTURE-CLI.md`.
This product is **Race/solve first**: Solving stays visible; main chat broadcasts during a run (**Enter** opens Send now / Queue; **`/queue …`** pre-selects Queue; empty Enter / Esc stops). **Send now** = force-followup interrupt on **all** providers (Cursor: cancel run + same-session `send`; Claude: `client.interrupt()`; Codex: `turn/interrupt`; Gemini: cancel in-flight generate) — work may restart. **Queue** = until idle (Cursor: idle turn; soft: next turn boundary); does **not** interrupt long builds/tools (blutter/ninja, scans). Agent pages stay chatable (target that agent).

## Flow

1. Launch TUI (`uv run artemis` / `chassis/bin/artemis` / global `artemis` after install).
2. **Before TUI:** if Docker or L0 is missing, the launcher asks in the **terminal** whether to run **full** setup (L0 + packs + warm runtimes; live logs; TUI stays closed until done or declined). `ARTEMIS_SETUP_AUTO=1` forces setup; `ARTEMIS_SKIP_LAUNCH_SETUP=1` skips. Pack caches and warm images are optional for the gate. If Docker/L0 is still missing after decline, the in-TUI **Install sandbox** dialog opens and blocks load/solve until Install finishes (`setup_status` / `setup_install`). In-TUI Install builds **L0 only** (`packs=[]`, `skip_warm_runtime=true`); packs attach on demand. Full bake / warm images: `artemis setup` or `artemis setup --full` (or the terminal full-setup prompt).
3. Auth Cursor / Claude / Codex / Gemini via `/connect` (or keys). Unavailable providers stay out of the picker.
4. Drop challenge flexibly in one message:
   - **Paste** description / `nc` / **web challenge links** (links stay in text — not downloaded).
   - **One or many folders** and/or **one or many files** (paths → challenge root or materialized workspace + `distfiles/`).
   - Mix paste + paths freely. Missing paths error as `path not found` (never “not a directory” for files).
   Main chat stays **sendable** during Solving: free text / `/queue …` → **Send to all agents?** (does **not** stop the agent). On an agent page the same flow targets that agent only (chat unlocked). Challenge reload (paths / large paste) or `/restart` / `/new` / `/clear` with active work → confirm stop+clear, then continue.
5. **Dialog** — flags required (default 1). No file config. Skipped after a failed load; stale prior `challenge_dir` must not reopen Flags until a successful load (or a new valid path).
6. **Dialog** — Single vs Swarm (same style as model picker).
7. **Dialog** — models (single pick or multi-select for swarm); only authenticated providers.
8. Daemon `swarm_start` with `flags_required` + `models[]` (requires an existing challenge directory). Live logs via socket → OpenCode-native tool/command rendering.
9. **Swarm UI (multi-model):** main page shows one box per agent (from `swarm_roster`). Always shows **page: main | agent (i/n)**. On **main**: ↑↓ = prompt history / scroll; **Tab** arms swarm select (↑↓ + Enter open). On an **agent page**, ↑↓ select · Enter open; **chat stays unlocked** (targets that agent; Esc → main). Global outcomes + **inline flag confirm (y/n)** stay on main. Soft race: `ACCEPTED (n/m)` keeps siblings; `CORRECT` cancels all. **Quota:** SDK usage-limit prints immediately; all-Cursor (or single) emits `[artemis] outcome` and cancels siblings; mixed Cursor+other prints a global `[artemis] outcome WARN` (siblings continue) plus per-agent detail. While swarm is still running, sibling boxes that have **not started yet** stay `starting…` — they must **not** all paint as `usage limit · failed` just because one agent hit quota (not-started ≠ no-quota). After `swarm_exit`, account quota may mark the grid failed. Blocked-provider / mojibake think streams are suppressed (one short line) so `# Artemis` is not flooded. **Sticky status:** live `Solving · elapsed` (and Esc stop) stays **above the prompt** for single- and multi-agent, and is the **only** live copy — the scrolled tool block keeps a static marker, agent boxes show no elapsed. Elapsed comes from the daemon on reconnect, so a TUI restart mid-run resumes the real time instead of counting from zero. **Solving unlock vs process alive:** terminal outcome unlocks chat/`Solving` but `swarmRunning` stays true until `swarm_exit` so **Esc / `/stop` still work** during teardown. Respawn under the supervisor lock does **not** emit an intermediate `swarm_exit`. **Esc cascade** (bound only when actionable): leave select → back to main → stop while process alive.
   **Mid-solve chat:** on **main**, free text opens **Send to all agents?** (broadcast fan-out). On an **agent page**, chat is unlocked and opens **Send to that agent?** only. **Send now** = force-followup interrupt (all providers); **Queue** = until idle (Cursor: idle turn; soft: next turn boundary). After **CORRECT**, during **Writeup** (before Hold banner) notes park as **queue** for the winner (`no_fanout`; toast “Queued for Hold Q&A”; sticky Operator queue bar). Once **Hold** is active, Enter sends **steer** to the winner (footer **Hold · Q&A** / **Hold · answering…**). Optional **`/queue <note>`** (bare `/queue` prompts for the note text). Empty Enter / Esc / `/stop` releases/stops.
   **Post-CORRECT hold:** siblings stop; the winning (or single) solver session stays up for Q&A (`[artemis] hold` / `[artemis] qa-wait` / `[artemis] qa →agent: …`). Footer shows **Hold · Q&A** (spinner only while **answering** / writeup / solving); the solve elapsed timer **freezes** at first CORRECT/quota and stays frozen across reconnect/replay. Operator notes render like OpenCode user bubbles (left border + panel; Queue badge inside). Q&A replies coalesce into an assistant-style block with `▣ <agent> · Q&A`. Post-CORRECT bash/tool/result dumps are hidden on the main feed (Hold is conversational). Writeup/Q&A turns mute solver `live()` tool spam. Single-agent / post-CORRECT sends target that agent (never a false `→ all`). Disable with `ARTEMIS_SKIP_SOLVER_HOLD=1`.
10. Flag confirm per candidate → continue until N → **one** summary (no duplicate outcome lines). A pending confirm survives a TUI **reconnect** (grace window before the daemon cancels) and is answered/declined — never silently dropped — so the swarm neither hangs nor loses the operator's answer.
   **Rejecting asks why:** `y` accepts instantly; `n` opens a one-line box (**enter** send · **esc** skip, blank is fine) and the text rides back to the solver as `REJECTED by operator — "…" not confirmed. Operator says: <reason> Continue hunting.` The operator is the only correctness oracle here (no CTFd), so a bare "no" left the solver unable to tell a wrong flag from a wrong technique — it would resubmit variants or decide the checker was broken. While the box is open the `y`/`n` bindings are disabled so the letters reach the input.
   **Attribution:** the accept line names the submitter (`CORRECT — accepted "…" via cursor/grok-4.5`), `FLAG FOUND:` carries the flag **and** `(solved by <agent>)` on one line (never rich-wrapped away from its label), the winning agent's box shows `✓ solved this challenge` (detail line: `solved · flag accepted`, or `solved · hold/Q&A` while the Hold process lingers), and the run ends with a **How the flag was found** recap in the main chat: winner + model spec + a prose **How:** block from `produce_writeup()` (same session, no tools). Post-CORRECT ai/think chatter is suppressed so the recap is not duplicated. The recap ships as `[artemis] summary …` lines; its first line (`Solved by <agent>[, <agent>]`) is what marks the winning boxes.
11. After summary (or after quota/stop) chat stays on **main**. Mid-solve free text uses **Send to all agents?** / per-agent send (steer/queue). **Challenge reload** or `/restart` / `/new` / `/clear` while work is active → confirm **stop current work** → clear daemon session + swarm UI + **new chat session** → prompt auto-resends once (reload path). `/stop` confirms stop only (no clear).
12. Performance: happy path uses **daemon NDJSON only** (bridge spawn is fallback if socket is down).

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
- Soft race: main page chat **broadcasts** to all running agents; agent pages keep chat **unlocked** and target that agent only. Esc → main still works for navigation.
- Chat sendable on main during Solving: **Enter** opens **Send to …?** — **Send now** = force-followup interrupt on all providers (Cursor cancel+send; Claude/Codex/Gemini soft-steer interrupt), then **resume** solve. **Queue** = until idle for all providers (Cursor: non-force idle; soft: next turn boundary). Pending Queue notes stay in a sticky panel; **Send now does not swallow Queue**. During **Writeup** (post-CORRECT, before Hold), Enter parks notes as queue for the winner (sticky Queue still visible). During **Hold**, Enter steers to the winner with no delivery dialog. Empty Enter / Esc / `/stop` stops (release hold; no clear). Solve-flow dialogs still block. `/restart` / `/new` / `/clear` confirm when residue/active; `/stop` confirms stop only. All four paths share one residue/active check (`util/artemis-solve-state.ts`) and never dismiss the open solve gate.
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
