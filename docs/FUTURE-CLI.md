# Future CTF Agent CLI — product notes

Living plan for turning the current `ctf-solve` race harness into a conversational
agent CLI (**Artemis**-shaped: run the agent, dump a challenge, optionally pick a
mode), while keeping autonomous race as a first-class mode.

Status: planning. Current product remains `uv run ctf-solve …`.
**Do not implement the CLI yet** — this doc is the plan only.

Multi-flag stop condition in the harness today: **`flags_required: N` (default 1)**.
Nth distinct accepted `submit_flag` → `CORRECT`. No `finish_challenge` tool.

---

## 1. Why this exists

Today we optimize for **autonomous race**: spawn solvers, recover flag(s),
`submit_flag` until N accepts → `CORRECT`, end the run.

That is a bad fit for a **chat CLI** where the user steers, sees answers in the
transcript, and decides what “done” means.

We need both — without forcing Race stop conditions onto Agent chat sessions.

---

## 2. Product UX target (Artemis)

Intended end-user flow (no mandatory challenge-folder editing):

```text
artemis                 # or: ctf
→ paste / drop challenge text (and optional files)
→ Flags required? [1]   # ask once; Enter = 1; HTB user+root → type 2
→ maybe pick a mode (Ask / Agent / Race)
→ run
```

**Non-goals for default UX**
- User must not be forced to edit `challenge.txt`.
- Mode choice stays coarse (`ask` / `agent` / `race`).

**CLI owns the count prompt** (planned — not built yet):
- Prompt: `Flags required? [1]`
- Default **1** on empty Enter
- User types `2` (or N) for multi-flag boxes
- Optional later: `--flags-required N` / advanced override
- File-based Race may still use `flags_required: N` in `challenge.txt` when present

Do **not** rely on brittle keyword inference from free-form paste (“Submit User Flag”, …)
as the sole stop condition — wording drifts; the CLI question is the honest UX.

---

## 3. Modes (Cursor-like, CTF-shaped)

Drop Plan (not the primary CTF need). Ship three modes:

| Mode | UX | Tools / sandbox | When the run “ends” | Flag gate |
|------|----|-----------------|---------------------|-----------|
| **Ask** | Q&A, explain writeups / code | Read-mostly; no (or minimal) exec | User stops | None — answer in chat |
| **Agent** | Interactive solve, user can interrupt / redirect | Full sandbox + tools | User stops, or agent reports done in chat | Soft: print candidates; optional `submit_flag` without killing session |
| **Race** | Multi-model parallel (today’s `ctf-solve`) | Full sandbox + tools | First `CORRECT` / budget / timeout | Yes — N distinct `submit_flag` accepts (or real scoreboard) |

Mode switch should be explicit (`/ask`, `/agent`, `/race` or CLI flags), not inferred.

### Agent vs Race (same brain, different stop rules)

Solve pipeline is largely shared (sandbox, packs, tools, flag plausibility). Differences:

| | **Agent** | **Race** |
|--|-----------|----------|
| Models | Usually one conversational session | Many models in parallel |
| After one ACCEPTED flag | Session can continue; user can steer (“get root next”) | Swarm keeps running until N → `CORRECT` |
| On `CORRECT` | Prefer **not** hard-kill the chat session (Artemis) | **Kill sibling solvers** — race round is over |
| “Talk more after partial progress” | First-class | Only if Race has not finished; after finish → new run / new spawn |

Do not invent a separate “multi-flag solver stack” for Agent vs Race — share
`backend/flags.py` and tool names; diverge only on session lifecycle.

### Mode details

**Ask**
- Fast answers, architecture questions, “what does this binary do”.
- Must not burn race budget or claim `FLAG FOUND`.

**Agent**
- Same brain + sandbox as Race, but conversational.
- Success = user sees the flag / solution in the reply.
- Soft `submit_flag` records a *candidate*; session stays up so the user can push for remaining flags (e.g. HTB user then root). For Agent, the CLI count may still inform progress display (`1/2`) without killing the chat on early CORRECT — product detail TBD.

**Race**
- Keep current swarm / coordinator behavior.
- Finish line = `CORRECT` after **N** distinct accepts (`flags_required`, default 1).
- Needs a real or heuristic verifier (see §5).

---

## 4. Multi-flag policy (HTB user/root, etc.)

### Problem
Machines / some CTFs award **more than one** flag. Paste text varies. Artemis users
will not always annotate counts in files.

### Decision (current + planned CLI)
1. **Harness today**: `flags_required: N` in challenge text (default **1**). Nth accept → `CORRECT`.
2. **No `finish_challenge`** — finish-only was rejected (models forget → hangs; early finish → wrong stop).
3. **No keyword-only inference** as the primary stop (format-overfit / wording drifts).
4. **Artemis CLI (planned, not implemented)**: after paste, ask `Flags required? [1]`.
   That value feeds the same N-count path as file-based Race.
5. Optional `flags_required: N` in `challenge.txt` remains for file-based / power-user Race.

### Risks (accepted)
- Wrong N (user says 1, machine needs 2) → early `CORRECT` (mitigate in Agent by not killing session; in Race by user re-run with N=2).
- Wrong N (user says 2, only one flag) → run waits until timeout/budget (mitigate with bumps / timeouts / user abort).

### Agent / Artemis mitigation
- After `ACCEPTED`, keep chatting; user can say “continue for root”.
- Treat local `CORRECT` as “plausible complete” until a real verifier exists.

---

## 5. Flag acceptance lessons (MarsAnalytica / NorthSec 2018)

### What happened
- PIN `q4Eo-eyMq-1dd0-leKx` → binary printed `FLAG-l0rdoFb1Nq4EoeyMq1dd0leKx`.
- Raw `FLAG-…` was **rejected** by shape check (only `PREFIX{…}`).
- Model wrapped formats (`CTF{FLAG-…}`, then `FLAG{…}`) until the gate passed.
- So: answer was known earlier; Race finish line lagged on format.

### Product truths
1. Without an external scoreboard, `CORRECT` means **plausible**, not **verified**.
2. Models will **rewrite format** to pass the gate (workaround, not proof).
3. Shape-only accept can also end the run on a **wrong** wrapped guess.
4. Agent chat does **not** need this gate to be useful.

### Policy (current code: `backend/flags.py`)
Accept when plausible and non-decoy:
- `PREFIX{body}`
- `PREFIX-body` (e.g. `FLAG-…`, `NSEC-…`)
- compact formatless secret tokens (mixed charset)
- reject license/PIN-like segmented keys as freeform (avoid accepting the PIN as the flag)

Reject message should tell the agent: submit the **exact awarded string**; do not wrap just to pass checks.

Multi-flag: `ACCEPTED (n/m)` until n == m → `CORRECT`. Already-complete further submits → `ALREADY SOLVED`.

### Race hardening (future)
Best → weakest:

1. **Real verifier** — remote scoreboard check; incorrect does not end the run.
2. **Candidate vs confirmed** — shape accept = candidate; confirmed only with strong signal (e.g. challenge output `ACCESS GRANTED`, or user confirm in CLI).
3. **Guess limits** — cooldown + max submits (partially present for wrong submits).
4. **Local regression only** — optional exact `flag` file for known answers.

Do **not** treat brace-wrapping as verification.

---

## 6. Future CLI shape (high level)

```
artemis                # interactive shell (product name TBD)
artemis ask  "…"       # one-shot Ask
artemis agent          # paste challenge → Flags required? [1] → interactive Agent
artemis race           # today’s multi-model race (ctf-solve)

# transitional while building:
ctf / ctf-solve …
```

Shared underneath (already largely built):
- Challenge folder loader (`challenge.txt` + distfiles) **and** prompt-paste entry
- Docker L0 sandbox + additive packs
- Tool surface: bash, files, submit_flag, …
- Multi-backend brains: Cursor SDK / Claude / Codex / …

CLI owns: TUI/REPL, mode, **flags-required prompt**, session history, user confirmations.  
Brain owns: model loop.  
Sandbox owns: execution isolation.

### Session behavior
- **Ask/Agent**: long-lived session; user messages steer; do not auto-kill on first `ACCEPTED`.
- **Race**: fire-and-forget or watchable; kill siblings on `CORRECT`.

### Product split (from earlier strategy notes)
- **Local hands**: challenge files / paste, Docker, tool execution (customer machine).
- **Remote brain** (optional later): prompts / playbooks / routing stay off the shipped binary if IP matters.
- Zero-retention story for challenge/flag traffic if cloud brain is used.

---

## 7. OpenCode CLI vs Codex CLI — what to base on

This is **not** “which coding agent should a human use daily”.  
It is “what do we reuse for **our** CTF CLI shell / agent loop”.

### Already in this repo
- **Cursor SDK** — primary path; solved MarsAnalytica in testing.
- **Codex** — optional solver/coordinator via `codex` CLI / app-server (`OPENAI_API_KEY`).
- **Claude** — optional SDK coordinator/solver.
- OpenCode Zen endpoint appears as a model route option in config — not a full CLI product shell.

### Comparison (relevant to us)

| | **OpenCode CLI** | **Codex CLI** |
|--|------------------|---------------|
| Models | Multi-provider (BYOK, many vendors) | OpenAI-centric |
| Fit for **Race** (multi-model) | Strong — one harness, many brains | Weak as the *only* shell — one vendor |
| Custom tools / MCP | Flexible, open | Good, but ecosystem is OpenAI-shaped |
| Sandbox story | Weaker OS sandbox — **we already have Docker** | Strong OS sandbox — overlaps our Docker story |
| UX (TUI, sessions, modes) | Strong open TUI / session model to learn from | Polished; ChatGPT-tied |
| Lock-in | Low | Higher (OpenAI) |
| As a **dependency we fork** | MIT-friendly open harness | Possible, but fighting upstream product |

### Recommendation

**Do not pick either as “the whole product.”**  
Our moat is CTF orchestration + sandbox packs + Race — not another generic coding CLI.

Clarify the split (important):

| Layer | Choice |
|-------|--------|
| **CLI shell / UX chassis** | OpenCode-leaning (or our thin REPL). **Not** Codex CLI as the product frame. |
| **Model providers / Race brains** | Keep **Codex as a provider** (and Cursor, Claude, …). Removing Codex *CLI chassis* ≠ removing Codex *models*. |

Practical choice:

1. **Shell / UX → OpenCode-leaning**  
   Multi-provider, sessions, Ask / Agent / Race. Drop Codex CLI as the outer product skeleton.

2. **Keep `codex/*` as a Race/Agent provider** (already integrated) — GPT path stays.

3. **Keep Cursor SDK as a first-class backend** — proven on hard RE in our tests.

4. **Build our own thin REPL** on top of existing `backend/` rather than forking either CLI wholesale — unless OpenCode’s TUI/session layer is clearly cheaper to embed than rewrite. Spike (1–2 days): OpenCode-style UI + our tool router vs custom REPL calling existing solvers.

### Spike checklist (when we start CLI — not now)
- [ ] Prototype Ask/Agent REPL on existing Cursor solver (no Race).
- [ ] Paste-challenge entry (no mandatory `challenge.txt` edits).
- [ ] **Prompt `Flags required? [1]`** and wire into `flags_required` / swarm.
- [ ] Soft candidate flag in Agent (no session kill on `ACCEPTED` / early CORRECT).
- [ ] Race unchanged behind `artemis race` / `ctf-solve` (siblings die on `CORRECT`).
- [x] Document multi-flag: `submit_flag` → `ACCEPTED (n/m)` → Nth → `CORRECT`.
- [ ] Optional: spike OpenCode as UI-only front to our tool router.
- [ ] Optional: remote/real verify path for Race confirmed flags.
- [ ] Document mode semantics in `--help` / first-run tip.

---

## 8. Non-goals (for now)

- Implementing Artemis CLI in this repo pass (plan only).
- MarsAnalytica-specific skills or overfit prompts.
- Plan mode parity with Cursor.
- Replacing Docker sandbox with Codex OS sandbox.
- Treating local shape-accept as a scoreboard.
- `finish_challenge` as the Race finish line.
- Brittle keyword-only multi-flag detection as the sole stop condition.

---

## 9. One-line summary

**Artemis** = paste a challenge, ask **Flags required? [1]**, pick Ask/Agent/Race; **Ask** answers, **Agent** chats and can continue after partial flags, **Race** uses today’s N-count finish line (`CORRECT` kills siblings) — prefer **OpenCode-style multi-provider shell**, keep **Codex/Cursor as brains**, and never confuse “passed format check” with “flag is correct.”
