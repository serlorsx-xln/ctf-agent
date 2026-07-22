# Future Artemis CLI — product notes

Living plan for turning the current race harness into a normal agent CLI
(**Claude Code / OpenCode–shaped**): open a shell anywhere, point at a challenge,
answer a few prompts, then let it work — with Race still available as a mode.

Status: **planning only.** Current product remains `uv run artemis` / `ctf-solve`
(race). **Do not implement the interactive CLI yet** — this doc is the plan.

Multi-flag stop condition in the harness today: **`flags_required: N` (default 1)**.
Nth distinct accepted `submit_flag` → `CORRECT`. No `finish_challenge` tool.

---

## 1. Why this exists

Today we optimize for **autonomous race**: spawn solvers, recover flag(s),
`submit_flag` until N accepts → `CORRECT`, end the run.

That is a bad fit for day-to-day use: user opens a terminal anywhere, dumps a
challenge (path and/or paste), steers in chat, continues after partial progress,
and decides what “done” means.

We need both — without forcing Race stop conditions onto Agent chat sessions.

---

## 2. Product UX target (primary)

Feel like a normal coding agent CLI (Claude Code / similar), not a research
harness you must wire each time:

```text
# anywhere — cwd can be home, repo, or a challenge folder
$ artemis

→ interactive shell starts
→ user pastes challenge text, drops files, and/or points at a path:
     ./challenges/foo
     /tmp/htb-box
     (or: "here's the prompt + files are in ./dist")
→ Flags required? [1]     # Enter = 1; HTB user+root → 2
→ agent starts solving / chatting
→ user can interrupt, redirect, continue for the next flag, resume later
```

Also valid one-shots / shortcuts (same engine):

```text
artemis                              # open shell (default)
artemis /path/to/challenge           # open + load that folder
artemis agent "…"                    # optional: with initial message
artemis race --challenge ./foo       # today’s multi-model race
```

**Non-goals for default UX**
- User must not be forced to edit `challenge.txt` before starting.
- User must not need to be inside the Artemis repo.
- Mode choice stays coarse when needed (`ask` / `agent` / `race`); default = Agent.

**CLI owns the count prompt** (planned — not built yet):
- Prompt: `Flags required? [1]`
- Default **1** on empty Enter
- User types `2` (or N) for multi-flag boxes
- Optional later: `--flags-required N`
- File-based Race may still use `flags_required: N` in `challenge.txt` when present

Do **not** rely on brittle keyword inference from free-form paste as the sole
stop condition — wording drifts; the CLI question is the honest UX.

### Session behavior (must-have)

- Long-lived chat session (like Claude/OpenCode).
- Continue on the **same** challenge after partial progress (“got user, now root”).
- Resume / reopen previous sessions when useful.
- Soft `submit_flag` in Agent: record candidate / progress (`1/2`); **do not**
  kill the session on early accept.
- Race (if used): keep sibling-kill on `CORRECT` when N is met — that mode is a
  fire-and-forget round, not the daily chat UX.

---

## 3. Modes (CTF-shaped)

Drop Plan (not the primary CTF need). Ship three modes; **Agent is the default
daily path**.

| Mode | UX | Tools / sandbox | When the run “ends” | Flag gate |
|------|----|-----------------|---------------------|-----------|
| **Ask** | Q&A, explain writeups / code | Read-mostly; no (or minimal) exec | User stops | None — answer in chat |
| **Agent** | Interactive solve (default) | Full sandbox + tools | User stops, or agent reports done | Soft: candidates; optional `submit_flag` without killing session |
| **Race** | Multi-model parallel (today’s harness) | Full sandbox + tools | First `CORRECT` / budget / timeout | Yes — N distinct accepts |

Mode switch: explicit (`/ask`, `/agent`, `/race` or flags), not inferred.

### Agent vs Race (same brain, different stop rules)

| | **Agent** | **Race** |
|--|-----------|----------|
| Models | Usually one conversational session | Many models in parallel |
| After one ACCEPTED flag | Session continues; user steers | Swarm until N → `CORRECT` |
| On `CORRECT` | Prefer **not** hard-kill the chat | **Kill sibling solvers** |
| Resume / continue | First-class | New run / new spawn after finish |

Share `backend/flags.py` and tools; diverge only on session lifecycle.

---

## 4. Multi-flag policy

1. **Harness today**: `flags_required: N` in challenge text (default **1**).
2. **No `finish_challenge`** — rejected (models forget → hangs; early finish → wrong stop).
3. **No keyword-only inference** as the primary stop.
4. **Artemis CLI (planned)**: ask `Flags required? [1]` after challenge entry.
5. Optional `flags_required: N` in `challenge.txt` remains for file-based Race.

**Risks (accepted)**
- Wrong N=1 when machine needs 2 → early CORRECT in Race (mitigate: Agent keeps chatting; Race re-run with N=2).
- Wrong N=2 when only one flag → wait until timeout/abort.

---

## 5. Flag acceptance lessons (keep)

Without an external scoreboard, local `CORRECT` means **plausible**, not verified.
Models will rewrite format to pass shape gates. Agent chat does not need that
gate to be useful.

Policy stays in `backend/flags.py` (PREFIX{…}, PREFIX-…, compact tokens; reject
PIN-like decoys). Race hardening later: real verifier → candidate vs confirmed →
guess limits → optional local exact flag for regression only.

---

## 6. CLI chassis — OpenCode-first

**Decision:** base the interactive product shell on **OpenCode** (adapt / embed /
learn from), not invent a second generic coding CLI from scratch, and not use
Codex CLI as the outer product frame.

| Layer | Choice |
|-------|--------|
| **CLI shell / UX** | OpenCode-leaning (TUI, sessions, multi-provider). Adapt to Artemis flows. |
| **CTF orchestration** | Our `backend/` — sandbox, packs, flags, Race, tool router |
| **Model providers** | Keep Cursor / Claude / Codex / … as brains behind the shell |

Practical path when we start:

1. Spike: OpenCode-style (or fork) UI wired to our tool router + sandbox.
2. If embedding is cheaper than rewrite → prefer that; else thin REPL that
   copies OpenCode session UX patterns.
3. Keep Race behind `artemis race` / current aliases.

### Spike checklist (when we start CLI — not now)

- [ ] `artemis` alone opens interactive shell from any cwd
- [ ] Load challenge by path and/or paste + optional description
- [ ] Prompt `Flags required? [1]` → wire into `flags_required`
- [ ] Soft candidate flag in Agent (no session kill)
- [ ] Continue / resume same challenge session
- [ ] Race unchanged behind `artemis race` (siblings die on `CORRECT`)
- [x] Document multi-flag: `submit_flag` → `ACCEPTED (n/m)` → Nth → `CORRECT`
- [ ] Spike OpenCode as chassis in front of our tool router
- [ ] Document mode semantics in `--help` / first-run tip

---

## 7. Far future — product / SaaS (not next sprint)

When selling to customers, separate **brain** from **hands**:

```text
┌──────────────────────────┐         ┌─────────────────────────────┐
│  Our server (brain)      │  ←───→  │  Customer machine (hands)   │
│  • agent loop / prompts  │         │  • Artemis CLI or Web UI    │
│  • routing / IP playbooks│         │  • Docker L0 + L1 packs     │
│  • hand / billing        │         │  • bash / tools / files     │
│  • no full challenge dump│         │  • challenge files stay here│
│    in customer binary    │         │                             │
└──────────────────────────┘         └─────────────────────────────┘
```

Intent:

- **Brain on our side** — important prompts / orchestration / IP stay on the
  server; customer binary is a thin client + local executor.
- **Tools on customer machine** — challenge files and heavy tooling stay local
  (Docker sandbox / packs). Do not require uploading full challenge corpora
  unless the customer opts in.
- **Hand / billing** — metered usage for hosted brain.
- **Optional Web UI** — same protocol as the CLI (sessions, flag count, progress)
  for customers who prefer a browser.

This is **not** “run all CTF tools in our cloud” (old L3 rental idea — dropped).
Local execution remains the default; remote is the brain, not the sandbox.

Zero-retention / transit policy for any challenge snippets that must touch the
server TBD when this phase starts.

---

## 8. Non-goals (for now)

- Implementing Artemis interactive CLI in this repo pass (plan only).
- Customer Kali bridge / remote tool worker as sandbox layers (dropped — see
  `ARCHITECTURE-SANDBOX.md`: L0 + L1 only).
- MarsAnalytica-specific skills or overfit prompts.
- Plan mode parity with Cursor.
- Replacing Docker sandbox with Codex OS sandbox.
- Treating local shape-accept as a scoreboard.
- `finish_challenge` as the Race finish line.
- Brittle keyword-only multi-flag detection as the sole stop condition.

---

## 9. One-line summary

**Artemis** = type `artemis` anywhere → load a challenge (path and/or paste) →
ask **Flags required? [1]** → Agent chats and keeps going; Race stays as an
optional swarm mode — chassis **OpenCode-leaning**, brains Cursor/Claude/Codex,
far future **hosted brain + local hands** (+ optional web), never confuse
“passed format check” with “flag is correct.”
