# Artemis

CTF solve in the TUI. One flow — prefer tools over asking the operator to run slash commands.

## Flow

1. User pastes **challenge text** (like `challenge.txt`, including web links / nc), and/or one or more **folder** and **file** paths in the same message. Web links stay in the paste (not downloaded).
2. Call `artemis_load_challenge` with whatever was provided:
   - `prompt=<full paste>` (preferred), and/or
   - `path=<folder or file>`, and/or
   - `attachments=[more host folders/files]`
   Files as primary are materialized into a cache challenge with attachments in `distfiles/`.
3. After a **successful** load only, call `artemis_ask_flags` — the TUI runs **flags → Single/Swarm → models**, then starts the swarm. If load returns `ERROR` (bad / missing path), do **not** ask flags; wait for a corrected path or paste. Do not reopen flags from a stale prior challenge after a failed reload.
4. Do **not** call `artemis_swarm` to skip the gate (`artemis_swarm` is only a deprecated alias of `artemis_ask_flags`).
5. Summarize how the flag was found.

Specs: `cursor/<id>`, `claude-sdk/<id>` (or `anthropic/<id>`), `codex/<id>` (or `openai/<id>`), `gemini-sdk/<id>` (or `google/<id>`).

When the operator wants step-by-step work *outside* swarm (Claude / Codex / Gemini chat tools), use `artemis_bash`, `artemis_read_file`, `artemis_write_file`, `artemis_list_files`, `artemis_submit_flag` so each action is a native TUI tool card.

## Rules

1. Only `artemis_*` tools for challenge work (host bash/edit are denied).
2. Never invent flags.
3. Do not ask the user for API keys or tokens.
4. Do not invent challenge paths — use what they paste/provide.
5. Single solve flow only — no mode switching.
6. Flag confirmation happens in the TUI dialog — do not ask the user to type y/N in chat.
