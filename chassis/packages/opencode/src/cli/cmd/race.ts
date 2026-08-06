/**
 * Artemis CTF swarm — shells out to the Python swarm harness.
 * Credentials stay in env/config; this only picks models + challenge path.
 */
import type { CommandModule } from "yargs"
import { spawn } from "child_process"
import path from "path"

function pythonCmd(): { cmd: string; prefix: string[] } {
  const repo =
    process.env.ARTEMIS_REPO_ROOT ||
    (process.env.ARTEMIS_CHASSIS_ROOT
      ? path.resolve(process.env.ARTEMIS_CHASSIS_ROOT, "..")
      : process.cwd())
  return { cmd: "uv", prefix: ["run", "--directory", repo, "artemis", "swarm"] }
}

async function runSwarm(args: Record<string, unknown>) {
  const { cmd, prefix } = pythonCmd()
  const argv: string[] = [...prefix]
  if (args.challenge) {
    argv.push("--challenge", String(args.challenge))
  }
  const models = (args.models as string[] | undefined) || []
  for (const m of models) {
    argv.push("--models", m)
  }
  if (args["flags-required"] != null) {
    argv.push("--flags-required", String(args["flags-required"]))
  }
  if (args.verbose) argv.push("-v")

  await new Promise<void>((resolve, reject) => {
    const child = spawn(cmd, argv, { stdio: "inherit", env: process.env })
    child.on("error", reject)
    child.on("exit", (code) => {
      if (code === 0) resolve()
      else reject(new Error(`artemis swarm exited ${code}`))
    })
  })
}

const builder = (yargs: import("yargs").Argv) =>
  yargs
    .positional("challenge", {
      type: "string" as const,
      describe: "Challenge directory",
    })
    .option("models", {
      type: "array" as const,
      string: true,
      describe: "Model specs (multi-provider). Keys must already be configured.",
    })
    .option("flags-required", {
      type: "number" as const,
      describe: "Distinct flags needed (default: from challenge.txt or 1)",
    })
    .option("verbose", {
      alias: "v",
      type: "boolean" as const,
      default: false,
    })

export const SwarmCommand: CommandModule = {
  command: "swarm [challenge]",
  describe: "Multi-model CTF swarm (Python; siblings die on CORRECT)",
  builder,
  handler: async (args) => runSwarm(args as Record<string, unknown>),
}

/** Hidden legacy alias — prefer `swarm`. */
export const RaceCommand: CommandModule = {
  command: "race [challenge]",
  describe: false,
  builder,
  handler: async (args) => runSwarm(args as Record<string, unknown>),
}
