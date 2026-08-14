/**
 * Artemis CTF plugin — load/gate/status tools + Cursor auth + official catalog.
 *
 * Chat does not solve. Sandbox solvers do. Happy path goes through the daemon
 * socket (no per-tool Python spawn). Bridge subprocess is fallback only.
 */
import { type Plugin, tool } from "@opencode-ai/plugin"
import { spawn } from "child_process"
import { existsSync } from "node:fs"
import path from "path"
import { daemon } from "../packages/tui/src/artemis/client"

function repoRoot(): string {
  return (
    process.env.ARTEMIS_REPO_ROOT ||
    path.resolve(process.env.ARTEMIS_CHASSIS_ROOT || process.cwd(), "..")
  )
}

/** Env for legacy bridge subprocesses (fallback only). */
function bridgeEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    ARTEMIS_FLAG_CONFIRM: process.env.ARTEMIS_FLAG_CONFIRM || "1",
  }
}

/** Prefer daemon request; fall back to bridge if socket is down. */
async function daemonOp(
  op: string,
  input?: Record<string, unknown>,
  sessionID?: string,
): Promise<string> {
  if (sessionID) daemon.setSessionId(sessionID)
  try {
    const res = await daemon.request<{ text?: string; ok?: boolean; error?: string }>(
      op,
      input || {},
    )
    if (res && typeof res === "object") {
      if (res.ok === false && res.error) throw new Error(String(res.error))
      if (typeof res.text === "string") return res.text.trim() || "(ok)"
    }
    return "(ok)"
  } catch {
    const env = bridgeEnv()
    if (sessionID) env.ARTEMIS_SESSION_ID = sessionID
    return await runPython(op, input, env)
  }
}

function bridgeArgv(root: string): { cmd: string; args: string[] } {
  const py =
    process.platform === "win32"
      ? path.join(root, ".venv", "Scripts", "python.exe")
      : path.join(root, ".venv", "bin", "python")
  if (existsSync(py)) {
    return { cmd: py, args: ["-m", "backend.shell.bridge"] }
  }
  const uv =
    process.platform === "win32"
      ? path.join(process.env.USERPROFILE || "", ".local", "bin", "uv.exe")
      : "uv"
  if (process.platform === "win32" && existsSync(uv)) {
    return { cmd: uv, args: ["run", "--directory", root, "python", "-m", "backend.shell.bridge"] }
  }
  return { cmd: "uv", args: ["run", "--directory", root, "python", "-m", "backend.shell.bridge"] }
}

function runPython(
  op: string,
  input?: Record<string, unknown>,
  env: NodeJS.ProcessEnv = bridgeEnv(),
): Promise<string> {
  const root = repoRoot()
  const payload = JSON.stringify(input || {})
  const { cmd, args } = bridgeArgv(root)
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, [...args, op], { env })
    let out = ""
    let err = ""
    child.stdout.on("data", (d) => (out += d.toString()))
    child.stderr.on("data", (d) => (err += d.toString()))
    child.stdin.write(payload)
    child.stdin.end()
    child.on("error", reject)
    child.on("exit", (code) => {
      if (code === 0) resolve(out.trim() || "(ok)")
      else reject(new Error(err || out || `exit ${code}`))
    })
  })
}

function readSession(): Record<string, unknown> {
  // The daemon pushes session state; read the current snapshot synchronously.
  // Falls back to {} if the daemon hasn't connected yet.
  return { ...(daemon.sessionState[0]() as Record<string, unknown>) }
}

type CursorModelItem = {
  id: string
  displayName?: string
  aliases?: string[]
}

async function fetchOfficialCursorModels(apiKey: string): Promise<CursorModelItem[]> {
  // Official Cloud Agents API: GET https://api.cursor.com/v1/models
  const res = await fetch("https://api.cursor.com/v1/models", {
    headers: {
      Authorization: `Bearer ${apiKey}`,
      Accept: "application/json",
    },
  })
  if (!res.ok) {
    // Basic auth fallback (documented alternative)
    const basic = Buffer.from(`${apiKey}:`).toString("base64")
    const res2 = await fetch("https://api.cursor.com/v1/models", {
      headers: {
        Authorization: `Basic ${basic}`,
        Accept: "application/json",
      },
    })
    if (!res2.ok) throw new Error(`Cursor models HTTP ${res.status}/${res2.status}`)
    const body2 = (await res2.json()) as { items?: CursorModelItem[] }
    return body2.items ?? []
  }
  const body = (await res.json()) as { items?: CursorModelItem[] }
  return body.items ?? []
}

function modelTemplate(id: string, name: string, existing?: Record<string, unknown>) {
  const base = (existing ?? {}) as Record<string, unknown>
  return {
    ...base,
    id,
    name,
    providerID: "cursor",
    api: {
      ...(typeof base.api === "object" && base.api ? (base.api as object) : {}),
      id,
      npm: "@ai-sdk/openai-compatible",
      url: "http://127.0.0.1:18765/v1",
    },
    status: "active",
    headers: {},
    options: {},
    cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
    limit: { context: 200_000, output: 64_000 },
    capabilities: {
      temperature: true,
      reasoning: false,
      attachment: false,
      toolcall: true,
      input: { text: true, audio: false, image: false, video: false, pdf: false },
      output: { text: true, audio: false, image: false, video: false, pdf: false },
      interleaved: false,
    },
    release_date: "",
    variants: {},
  }
}

async function startSolveFlowTool(
  defaultFlags: number | undefined,
  ctx: { sessionID: string; metadata: (input: { title: string; metadata: { output: string } }) => void },
) {
  daemon.setSessionId(ctx.sessionID)
  const title = "Solve"
  ctx.metadata({ title, metadata: { output: "TUI flow gate: flags → mode → models → swarm…\n" } })
  try {
    await daemon.request("solve_flow_start", { default: defaultFlags })
  } catch (e) {
    const output = `ERROR: ${(e as Error).message}`
    return { title, output, metadata: { title, output } }
  }
  const output = "TUI flow gate: flags → mode → models → swarm…"
  return { title, output, metadata: { title, output } }
}

export const ArtemisCtfPlugin: Plugin = async () => {
  // Connect to the control-plane daemon early (best-effort; reconnects on its own).
  daemon.ensureConnected().catch(() => {})
  return {
    /** Tag Cursor stub requests with the TUI session so load/ask_flags
     *  do not inherit another window's challenge_dir from `_default`. */
    "chat.headers": async (input, output) => {
      const sid = input?.sessionID
      if (typeof sid === "string" && sid.trim()) {
        output.headers = {
          ...(output.headers || {}),
          "X-Artemis-Session-Id": sid.trim(),
        }
      }
    },

    event: async ({ event }) => {
      // New OpenCode chat must not inherit stale ~/.cache/artemis/session.json
      // (flags 1/1 + flags_explicit skipping the digits dialog).
      if (event.type === "session.created") {
        try {
          const info = (event.properties as { info?: { id?: string; parentID?: string } })?.info
          // Child/subagent sessions must not rebind the process-wide daemon
          // peer or unlock a live swarm on the parent chat.
          if (info?.parentID) return
          if (daemon.swarmRunning[0]()) return
          // Late session.created must not drop busy while flags→models is open.
          if (daemon.solveFlowBusy[0]()) return
          const sid = info?.id
          if (sid) daemon.setSessionId(sid)
          // Do not clear_session here. TUI already cleared before create, and
          // a late clear races the resubmit load on the new chat.
          daemon.setFlowCompleted(false)
          daemon.setSolveLocked(false)
          daemon.setSolveFlowBusy(false)
          daemon.setSuppressSolveGate(false)
        } catch {
          /* daemon down — TUI clearSolvedSession already handled disk state */
        }
      }
    },

    auth: {
      provider: "cursor",
      methods: [
        {
          type: "api",
          label: "Cursor API key",
          prompts: [
            {
              type: "text",
              key: "key",
              message: "Cursor API key (cursor.com/dashboard → API Keys / Integrations)",
              placeholder: "cursor_... or crsr_...",
            },
          ],
        },
      ],
    },

    provider: {
      id: "cursor",
      async models(provider, ctx) {
        const key =
          (ctx.auth?.type === "api" ? ctx.auth.key : undefined) ||
          (process.env.CURSOR_API_KEY || "").trim() ||
          undefined

        const existingAuto = provider.models["default"] ?? provider.models["auto"]

        if (!key) {
          return {
            default: modelTemplate(
              "default",
              "Auto",
              existingAuto as Record<string, unknown> | undefined,
            ),
          } as typeof provider.models
        }

        try {
          const items = await fetchOfficialCursorModels(key)
          if (!items.length) throw new Error("empty catalog")
          const out: Record<string, (typeof provider.models)[string]> = {}
          for (const item of items) {
            const id = item.id
            if (!id) continue
            out[id] = modelTemplate(
              id,
              item.displayName || id,
              provider.models[id] as Record<string, unknown> | undefined,
            ) as (typeof provider.models)[string]
          }
          return out
        } catch {
          return {
            default: modelTemplate(
              "default",
              "Auto",
              existingAuto as Record<string, unknown> | undefined,
            ),
          } as typeof provider.models
        }
      },
    },

    "experimental.chat.system.transform": async (_input, output) => {
      const st = readSession()
      const lines = [
        "You are Artemis chat. You do not solve CTF challenges.",
        "The TUI loads the paste and opens flags → mode → models. Sandbox solvers do all work after Start.",
        "Never inspect files. Never list directories. Never run bash. Never submit flags.",
        "Call artemis_load_challenge only if the TUI did not load and the user pasted a challenge or path.",
        "Call artemis_ask_flags only if a challenge is loaded and the TUI gate did not appear.",
        "If load returns ERROR, tell the user to fix the path. Do not ask_flags.",
        "After the swarm finishes, summarize only if the user asks. Never invent flags. Do not ask for API keys.",
      ]
      if (st.challenge_dir) {
        lines.push(`Active challenge_dir: ${st.challenge_dir}`)
        lines.push(`Challenge name: ${st.challenge_name || "?"}`)
        const acc = (st.accepted_flags as string[]) || []
        lines.push(`accepted_flags: ${acc.join(" | ") || "(none)"}`)
        lines.push("Challenge is loaded. Do not use tools. Do not think about the files. Stop.")
      }
      output.system.push(lines.join("\n"))
    },

    tool: {
      artemis_load_challenge: tool({
        description:
          "Load a CTF challenge. Prefer prompt= pasted challenge.txt-style text (+ URLs). " +
          "Optional path= existing folder. Optional attachments= host file paths to copy into distfiles/. " +
          "Creates ~/.cache/artemis/challenges/<id>/ when using prompt. " +
          "A successful load opens the TUI flags → mode → models gate automatically.",
        args: {
          path: tool.schema.string().optional().describe("Host path to an existing challenge folder"),
          prompt: tool.schema
            .string()
            .optional()
            .describe("Full pasted challenge text (description, links, nc, flags_required)"),
          description: tool.schema.string().optional().describe("Alias of prompt"),
          name: tool.schema.string().optional().describe("Optional display/slug name"),
          attachments: tool.schema
            .array(tool.schema.string())
            .optional()
            .describe("Host paths to copy into distfiles/"),
          flags_required: tool.schema.number().optional().describe("Distinct flags needed (default from text or 1)"),
          mode: tool.schema.enum(["artemis"]).optional(),
        },
        async execute(args, ctx) {
          return await daemonOp("load", args, ctx.sessionID)
        },
      }),
      artemis_ask_flags: tool({
        description:
          "Start the CTF solve flow. The TUI runs flags → mode → models, then starts the swarm.",
        args: {
          default: tool.schema.number().optional().describe("Default if Esc / timeout (1–64)"),
        },
        async execute(args, ctx) {
          return await startSolveFlowTool(args.default, ctx)
        },
      }),
      artemis_status: tool({
        description: "Show Artemis session state (challenge, flags progress, mode).",
        args: {},
        async execute(_args, ctx) {
          return await daemonOp("status", {}, ctx.sessionID)
        },
      }),
      artemis_stop_sandbox: tool({
        description: "Stop the active Docker CTF sandbox and any running swarm (frees resources).",
        args: {
          challenge_dir: tool.schema.string().optional(),
        },
        async execute(args, ctx) {
          return await daemonOp("sandbox_stop", args, ctx.sessionID)
        },
      }),
      artemis_stop_swarm: tool({
        description: "Stop the running CTF swarm (use after esc interrupt if a previous run is stuck).",
        args: {},
        async execute(_args, ctx) {
          return await daemonOp("swarm_stop", {}, ctx.sessionID)
        },
      }),
      artemis_swarm: tool({
        description:
          "Deprecated alias — same as artemis_ask_flags. Opens the TUI gate " +
          "(flags → mode → models) then starts the swarm. Do NOT pass models; the TUI picker chooses.",
        args: {
          default: tool.schema.number().optional().describe("Default flags if Esc / timeout (1–64)"),
        },
        async execute(args, ctx) {
          return await startSolveFlowTool(args.default, ctx)
        },
      }),
    },
  }
}

/** Modern plugin export shape preferred by the chassis loader. */
export default {
  id: "artemis-ctf",
  server: ArtemisCtfPlugin,
}
