import yargs, { type Argv } from "yargs"
import { hideBin } from "yargs/helpers"
import { UI } from "./cli/ui"
import { InstallationVersion } from "@opencode-ai/core/installation/version"
import { FormatError } from "./cli/error"
import { EOL } from "os"
import { errorMessage } from "./util/error"
import { Heap } from "./cli/heap"

const args = hideBin(process.argv)

const SUBCOMMANDS = new Set([
  "attach",
  "run",
  "debug",
  "providers",
  "auth",
  "agent",
  "serve",
  "models",
  "session",
  "swarm",
  "race",
  "completion",
])

function loadAllCommands(argv: string[]): boolean {
  if (argv.includes("-h") || argv.includes("--help") || argv.includes("-v") || argv.includes("--version")) {
    return true
  }
  const first = argv[0]
  return Boolean(first && !first.startsWith("-") && SUBCOMMANDS.has(first))
}

function show(out: string) {
  const text = out.trimStart()
  if (!text.startsWith("opencode ") && !text.startsWith("artemis ")) {
    process.stderr.write(UI.logo() + EOL + EOL)
    process.stderr.write(text + EOL)
    return
  }
  process.stderr.write(out)
}

let cli: Argv = yargs(args)
  .parserConfiguration({ "populate--": true })
  .scriptName("artemis")
  .wrap(100)
  .help("help", "show help")
  .alias("help", "h")
  .version("version", "show version number", InstallationVersion)
  .alias("version", "v")
  .option("print-logs", {
    describe: "print logs to stderr",
    type: "boolean",
  })
  .option("log-level", {
    describe: "log level",
    type: "string",
    choices: ["DEBUG", "INFO", "WARN", "ERROR"],
  })
  .option("pure", {
    describe: "run without external plugins",
    type: "boolean",
  })
  .middleware(async (opts) => {
    if (opts.printLogs) process.env.OPENCODE_PRINT_LOGS = "1"
    if (opts.logLevel) process.env.OPENCODE_LOG_LEVEL = opts.logLevel
    if (opts.pure) {
      process.env.OPENCODE_PURE = "1"
    }

    Heap.start()

    process.env.AGENT = "1"
    process.env.OPENCODE = "1"
    process.env.ARTEMIS = "1"
    process.env.OPENCODE_PID = String(process.pid)
  })
  .usage("Artemis — CTF agent CLI")
  .completion("completion", "generate shell completion script")

const { TuiThreadCommand } = await import("./cli/cmd/tui")
cli = cli.command(TuiThreadCommand)

if (loadAllCommands(args)) {
  const [attach, run, debug, providers, agent, serve, models, session, race] = await Promise.all([
    import("./cli/cmd/attach"),
    import("./cli/cmd/run"),
    import("./cli/cmd/debug"),
    import("./cli/cmd/providers"),
    import("./cli/cmd/agent"),
    import("./cli/cmd/serve"),
    import("./cli/cmd/models"),
    import("./cli/cmd/session"),
    import("./cli/cmd/race"),
  ])
  cli = cli
    .command(attach.AttachCommand)
    .command(run.RunCommand)
    .command(debug.DebugCommand)
    .command(providers.ProvidersCommand)
    .command(agent.AgentCommand)
    .command(serve.ServeCommand)
    .command(models.ModelsCommand)
    .command(session.SessionCommand)
    .command(race.SwarmCommand)
    .command(race.RaceCommand)
}

cli = cli
  .fail((msg, err) => {
    if (
      msg?.startsWith("Unknown argument") ||
      msg?.startsWith("Not enough non-option arguments") ||
      msg?.startsWith("Invalid values:")
    ) {
      if (err) throw err
      cli.showHelp(show)
    }
    if (err) throw err
    process.exit(1)
  })
  .strict()

try {
  if (args.includes("-h") || args.includes("--help")) {
    await cli.parse(args, (err: Error | undefined, _argv: unknown, out: string) => {
      if (err) throw err
      if (!out) return
      show(out)
    })
  } else {
    await cli.parse()
  }
} catch (e) {
  const formatted = FormatError(e)
  if (formatted) UI.error(formatted)
  if (formatted === undefined) {
    UI.error("Unexpected error" + EOL)
    process.stderr.write(errorMessage(e) + EOL)
  }
  process.exitCode = 1
} finally {
  process.exit()
}
