# Future CTF Agent CLI — product notes

Living plan for turning the current `ctf-solve` race harness into a conversational
agent CLI (Cursor-like), while keeping autonomous race as a first-class mode.

Status: planning only. Current product remains `uv run ctf-solve …`.

---

## 1. Why this exists

Today we optimize for **autonomous race**: spawn solvers, recover a flag, call
`submit_flag`, end the run on `CORRECT`.

That is a bad fit for a **chat CLI** where the user steers, sees answers in the
transcript, and decides what “done” means.

We need both — without forcing Race flag heuristics onto Agent chat.

---

## 2. Modes (Cursor-like, CTF-shaped)

Drop Plan (not the primary CTF need). Ship three modes:

| Mode | UX | Tools / sandbox | When the run “ends” | Flag gate |
|------|----|-----------------|---------------------|-----------|
| **Ask** | Q&A, explain writeups / code | Read-mostly; no (or minimal) exec | User stops | None — answer in chat |
| **Agent** | Interactive solve, user can interrupt / redirect | Full sandbox + tools | User stops, or agent reports done in chat | None required — print candidate in chat |
| **Race** | Multi-model parallel (today’s `ctf-solve`) | Full sandbox + tools | First confirmed flag / budget / timeout | Yes — `submit_flag` + plausibility (or real scoreboard) |

Mode switch should be explicit (`/ask`, `/agent`, `/race` or CLI flags), not inferred.

### Mode details

**Ask**
- Fast answers, architecture questions, “what does this binary do”.
- Must not burn race budget or claim `FLAG FOUND`.

**Agent**
- Same brain + sandbox as Race, but conversational.
- Success = user sees the flag / solution in the reply.
- Optional: soft `submit_flag` that records a *candidate* without ending the session.

**Race**
- Keep current swarm / coordinator behavior.
- `submit_flag` is the finish line (cost control).
- Needs a real or heuristic verifier (see §3).

---

## 3. Flag acceptance lessons (MarsAnalytica / NorthSec 2018)

### What happened
- PIN `q4Eo-eyMq-1dd0-leKx` → binary printed `FLAG-l0rdoFb1Nq4EoeyMq1dd0leKx`.
- Raw `FLAG-…` was **rejected** by shape check (only `PREFIX{…}`).
- Model wrapped formats (`CTF{FLAG-…}`, then `FLAG{…}`) until the gate passed.
- So: answer was known earlier; Race finish line lagged on format.

### Product truths
1. Without CTFd/scoreboard, `CORRECT` means **plausible**, not **verified**.
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

### Race hardening (future)
Best → weakest:

1. **Real verifier** — CTFd / remote check; incorrect does not end the run.
2. **Candidate vs confirmed** — shape accept = candidate; confirmed only with strong signal (e.g. challenge output `ACCESS GRANTED`, or user confirm in CLI).
3. **Guess limits** — cooldown + max submits (already partially there).
4. **Local regression only** — optional exact `flag` file for known answers.

Do **not** treat brace-wrapping as verification.

---

## 4. Future CLI shape (high level)

```
ctf                    # interactive shell
ctf ask  "…"           # one-shot Ask
ctf agent ./chal       # interactive Agent on a challenge
ctf race  ./chal       # today’s multi-model race (ctf-solve)
```

Shared underneath (already largely built):
- Challenge folder loader (`challenge.txt` + distfiles)
- Docker L0 sandbox + additive packs
- Tool surface: bash, files, submit_flag, …
- Multi-backend brains: Cursor SDK / Claude / Codex / …

CLI owns: TUI/REPL, mode, session history, user confirmations.  
Brain owns: model loop.  
Sandbox owns: execution isolation.

### Session behavior
- **Ask/Agent**: long-lived session; user messages steer; no auto-kill on flag shape.
- **Race**: fire-and-forget or watchable; kill siblings on confirmed flag.

### Product split (from earlier strategy notes)
- **Local hands**: challenge files, Docker, tool execution (customer machine).
- **Remote brain** (optional later): prompts / playbooks / routing stay off the shipped binary if IP matters.
- Zero-retention story for challenge/flag traffic if cloud brain is used.

---

## 5. OpenCode CLI vs Codex CLI — what to base on

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

### Spike checklist (when we start)
- [ ] Prototype Ask/Agent REPL on existing Cursor solver (no Race).
- [ ] Soft candidate flag in Agent (no session kill).
- [ ] Race unchanged behind `ctf race` / `ctf-solve`.
- [ ] Optional: spike OpenCode as UI-only front to our tool router.
- [ ] Optional: CTFd/real verify path for Race confirmed flags.
- [ ] Document mode semantics in `--help` / first-run tip.

---

## 6. Non-goals (for now)

- MarsAnalytica-specific skills or overfit prompts.
- Plan mode parity with Cursor.
- Replacing Docker sandbox with Codex OS sandbox.
- Treating local shape-accept as a scoreboard.

---

## 7. One-line summary

**Ask** answers, **Agent** chats and solves without a finish-line gate, **Race** keeps today’s multi-model finish line — prefer **OpenCode-style multi-provider shell**, keep **Codex/Cursor as brains**, and never confuse “passed format check” with “flag is correct.”
