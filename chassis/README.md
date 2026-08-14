# Artemis Chassis

Standalone terminal agent chassis for **Artemis**, forked from
[OpenCode](https://github.com/anomalyco/opencode) (MIT).

## Product decision (locked)

- Artemis is **standalone** — we do **not** `npm install opencode-ai` / shell out to
  a user-installed `opencode` binary as the product.
- OpenCode source lives **in this tree** under `chassis/` and is adapted for CTF
  (single solve flow, flag prompts, sandbox tools).
- Python `backend/` remains the CTF orchestration layer (Docker L0+L1 packs,
  multi-model swarm, flag accept). The chassis is the interactive TUI.

## Upstream

| Item | Value |
|------|--------|
| Upstream | https://github.com/anomalyco/opencode |
| Snapshot | OpenCode `1.18.4` (see `packages/opencode/package.json`) |
| License | MIT — full text in `LICENSE` (OpenCode copyright retained) |
| Included | TUI dependency closure only (orphans pruned; non-CTF CLI cmds removed) |

See `NOTICE` for attribution requirements.

## Layout

```text
chassis/
  bin/artemis           # launcher (Bun + opencode.json)
  package.json          # bun workspace root (Artemis-branded)
  opencode.json         # CTF agents / tool permissions
  artemis/plugin.ts     # CTF tools → Python bridge
  packages/opencode/    # main CLI / agent
  packages/tui/         # terminal UI
  packages/core/ …      # shared OpenCode packages (vendored)
```

## Build (requires Bun)

```bash
# Install Bun: https://bun.sh
cd chassis
bun install
./bin/artemis
# or:
bun run artemis
```

Python entry:

```bash
uv run artemis              # OpenCode TUI (requires Bun)
uv run artemis swarm …       # Python swarm harness
```

## CTF bridge

The TUI loads the paste and opens flags → mode → models. Chat does not solve.
Sandbox solvers run in Docker after Start.

Chassis tools call into Python:

- `artemis_load_challenge` / `artemis_ask_flags` / `artemis_status` → `backend.shell.bridge`
- `artemis swarm` → `python -m backend.cli swarm …` (swarm)

Do not reimplement pack/sandbox logic in TypeScript.
