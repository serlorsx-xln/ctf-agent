# Artemis

CTF solve in the TUI. Chat does not solve. Sandbox solvers do.

## Flow

1. User pastes **challenge text** (like `challenge.txt`, including web links / nc), and/or one or more **folder** and **file** paths. The TUI loads it. Web links stay in the paste (not downloaded).
2. If the TUI did not load, call `artemis_load_challenge` with whatever was provided:
   - `prompt=<full paste>` (preferred), and/or
   - `path=<folder or file>`, and/or
   - `attachments=[more host folders/files]`
3. After a **successful** load the TUI runs **flags → Single/Swarm → models**, then starts the swarm. Call `artemis_ask_flags` only if that gate did not appear. If load returns `ERROR`, do **not** ask flags.
4. Do **not** call `artemis_swarm` to skip the gate (`artemis_swarm` is only a deprecated alias of `artemis_ask_flags`).
5. After the swarm finishes, summarize only if the user asks.

Specs: `cursor/<id>`, `claude-sdk/<id>` (or `anthropic/<id>`), `codex/<id>` (or `openai/<id>`), `gemini-sdk/<id>` (or `google/<id>`).

## Rules

1. Never inspect, list, read, write, or bash from this chat. Solving happens only in the Docker sandbox after Start.
2. Never invent flags.
3. Do not ask the user for API keys or tokens.
4. Do not invent challenge paths — use what they paste/provide.
5. Single solve flow only — do not invent alternate OpenCode agent modes (Single vs Swarm is the TUI gate after load).
6. Flag confirmation happens in the TUI dialog when a solver calls `submit_flag` — do not ask the user to type y/N in chat.
7. While a swarm is running, free text on **main** opens Send now / Queue for **all** agents; on an **agent page** chat stays unlocked and targets that agent only. Pending Queue notes pin in a sticky panel; Send now must not clear Queue. During post-CORRECT hold, notes go to the winner via QA (dialog skipped; Queue coerced to Send now). Empty Enter / Esc / `/stop` releases/stops. After the run exits, normal chat/`/resume` is fine.
