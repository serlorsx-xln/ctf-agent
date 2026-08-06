// Help-text snapshots for every CLI command + key subcommand. Catches
// accidental flag removals, renames, and reordering in a single sweep —
// any change to the user-visible CLI surface shows up here as a diff.
//
// Snapshots are taken at COLUMNS=120 so wrapping is stable across
// terminal sizes. The default artemis tui command is excluded —
// `artemis --help` includes an ASCII banner that pulls in the install
// version (changes per release), so we'd snapshot a moving target.
import { describe, expect } from "bun:test"
import { Effect } from "effect"
import { cliIt } from "../../lib/cli-process"
import { normalizeForSnapshot, PATH_SEP } from "../../lib/snapshot"

function normalize(text: string): string {
  return normalizeForSnapshot(text, {
    pathReplacements: [
      [new RegExp(`<TMPDIR>${PATH_SEP}oc-cli-[A-Za-z0-9]+`, "g"), "<HOME>"],
      [/\s+\[string\] \[default: "<HOME>"\]/g, ' [string] [default: "<HOME>"]'],
    ],
  })
}

// Artemis product CLI surface (non-CTF upstream cmds pruned).
const TOP_LEVEL = [
  "attach",
  "run",
  "debug",
  "providers",
  "agent",
  "serve",
  "models",
  "session",
  "swarm",
] as const

const SUBCOMMANDS = [
  ["providers", "list"],
  ["providers", "login"],
  ["providers", "logout"],
  ["agent", "create"],
  ["agent", "list"],
  ["session", "list"],
  ["session", "delete"],
] as const

const SNAPSHOT_ENV = { COLUMNS: "120" }

describe("artemis CLI help-text snapshots", () => {
  cliIt.live(
    "every documented command emits stable help text",
    ({ opencode }) =>
      Effect.gen(function* () {
        const jobs = [
          ...TOP_LEVEL.map((cmd) => ({ label: cmd, argv: [cmd, "--help"] as string[] })),
          ...SUBCOMMANDS.map(([a, b]) => ({
            label: `${a} ${b}`,
            argv: [a, b, "--help"] as string[],
          })),
        ]

        yield* Effect.forEach(
          jobs,
          ({ label, argv }) =>
            Effect.gen(function* () {
              const result = yield* opencode.spawn(argv, { env: SNAPSHOT_ENV })
              opencode.expectExit(result, 0, label)
              expect(normalize(result.stdout + result.stderr)).toMatchSnapshot(label)
            }),
          { concurrency: 8 },
        )
      }),
    { timeout: 120_000 },
  )
})
