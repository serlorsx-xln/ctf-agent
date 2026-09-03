/**
 * Artemis daemon control-plane client (NDJSON over a Unix socket).
 *
 * Replaces the old file-polling control plane: the TUI holds one persistent
 * connection to the daemon, sends typed requests, and receives push events
 * (session/usage/dialog/swarm log). Sidebar panels and the swarm component read
 * SolidJS signals fed by these pushes instead of polling ~/.cache/artemis/*.json.
 *
 * Cost is provider-SDK-reported only — the daemon forwards what the swarm
 * subprocess reports; the client never estimates.
 */
import net from "node:net"
import fs from "node:fs"
import path from "node:path"
import os from "node:os"
import { createRoot, createSignal } from "solid-js"
import { eventsFromLogLine, coalesceEvents, dedupeEvents, hasTerminalSolveOutcome, type ArtemisEvent } from "../util/artemis-live-log"
import { agentKeyFromSpec, rosterFromDaemonPush, rosterFromSpecs } from "../util/artemis-swarm-agents"

export type ArtemisUsage = {
  tokens: number
  input: number
  output: number
  cache_read: number
  cost_usd: number | null
}

export type SessionState = {
  challenge_dir?: string
  challenge_name?: string
  container_id?: string
  flags_required?: number
  flags_explicit?: boolean
  accepted_flags?: string[]
  mode?: string
  updated_at?: number
}

export type FlagConfirmRequest = { request_id: string; flag: string }
export type FlagsAskRequest = { request_id: string; default: number; challenge?: string }
export type SolveFlowRequest = {
  default_flags: number
  challenge?: string
  preselected?: string[]
  /** True when the daemon opened the gate after a successful load. */
  from_load?: boolean
}

/** Whether a daemon push belongs to this TUI window's OpenCode session. */
export function pushBelongsToSession(
  msgSession: unknown,
  currentSessionId: string | null,
): boolean {
  if (msgSession == null || String(msgSession).length === 0) return true
  if (currentSessionId == null) return true
  return String(msgSession) === currentSessionId
}

type Envelope = {
  v: number
  id: string | null
  type: string
  session?: string | null
  [k: string]: unknown
}

function cacheRoot(): string {
  return process.env.ARTEMIS_CACHE || path.join(os.homedir(), ".cache", "artemis")
}

function socketPath(): string {
  return path.join(cacheRoot(), "daemon.sock")
}

function readDaemonPort(): number | null {
  const env = process.env.ARTEMIS_DAEMON_PORT
  if (env && /^\d+$/.test(env)) return parseInt(env, 10)
  try {
    const raw = fs.readFileSync(path.join(cacheRoot(), "daemon.port"), "utf8").trim()
    const p = parseInt(raw, 10)
    return Number.isFinite(p) ? p : null
  } catch {
    return null
  }
}

function createDaemonConnection(): net.Socket {
  const endpoint = (process.env.ARTEMIS_DAEMON_ENDPOINT || "").trim()
  if (endpoint.startsWith("tcp:")) {
    const rest = endpoint.slice(4)
    const idx = rest.lastIndexOf(":")
    const host = rest.slice(0, idx) || "127.0.0.1"
    const port = parseInt(rest.slice(idx + 1), 10)
    return net.createConnection({ host, port })
  }
  if (endpoint.startsWith("unix:")) {
    return net.createConnection(endpoint.slice(5))
  }
  const port = readDaemonPort()
  if (process.platform === "win32" && port) {
    return net.createConnection({ host: "127.0.0.1", port })
  }
  return net.createConnection(socketPath())
}

/** Max lines held in the TUI swarm-log buffer (matches daemon REPLAY_TAIL_LINES). */
const SWARM_LOG_CAP = 5000

// Module-level signals wrapped in createRoot so effects inside the TUI's
// reactive tree track them. (SolidJS signals created at import time without a
// root can be read/written, but effects that read them may not re-run on set —
// which is exactly the flags-ask dialog bug we hit.)
const rootSignals = createRoot(() => {
  const sessionState = createSignal<SessionState>({})
  const usage = createSignal<ArtemisUsage | null>(null)
  const flagConfirm = createSignal<FlagConfirmRequest | null>(null)
  const swarmEvents = createSignal<ArtemisEvent[]>([])
  const swarmRunning = createSignal<boolean>(false)
  const solveLocked = createSignal<boolean>(false)
  const flowCompleted = createSignal<boolean>(false)
  /** After quota/CORRECT/exit: block auto ask_flags gate until the user types or /restart. */
  const suppressSolveGate = createSignal<boolean>(false)
  const lastModels = createSignal<string[]>([])
  const lastFlagsRequired = createSignal<number>(1)
  /** Stable agent labels for multi-swarm boxes (from daemon roster or startSwarm). */
  const swarmRoster = createSignal<string[]>([])
  /** null = main page; otherwise focused agent key. */
  const swarmFocus = createSignal<string | null>(null)
  /** Cursor in swarm page list (0 = main, 1..n = agents). */
  const swarmNavCursor = createSignal<number>(0)
  /** Tab-armed: ↑↓ select swarm pages on main without stealing prompt history. */
  const swarmNavArmed = createSignal<boolean>(false)
  /** True while flags→mode→models dialogs are open (ignore stale replay). */
  const solveFlowBusy = createSignal<boolean>(false)
  /** Wall-clock ms when current swarm run started (live elapsed timer). */
  const swarmStartedAt = createSignal<number | null>(null)
  /** Wall-clock ms when solve elapsed should freeze (first terminal outcome / exit). */
  const swarmEndedAt = createSignal<number | null>(null)
  /** Monotonic per-agent parsed-line counts (never decreases during a run). */
  const agentLineCounts = createSignal<Record<string, number>>({})
  /** null = unknown; false = must install; true = ready to solve. */
  const setupReady = createSignal<boolean | null>(null)
  const setupMessage = createSignal<string>("")
  const setupInstalling = createSignal<boolean>(false)
  const setupLogs = createSignal<string[]>([])
  return {
    sessionState,
    usage,
    flagConfirm,
    swarmEvents,
    swarmRunning,
    solveLocked,
    flowCompleted,
    suppressSolveGate,
    lastModels,
    lastFlagsRequired,
    swarmRoster,
    swarmFocus,
    swarmNavCursor,
    swarmNavArmed,
    solveFlowBusy,
    swarmStartedAt,
    swarmEndedAt,
    agentLineCounts,
    setupReady,
    setupMessage,
    setupInstalling,
    setupLogs,
  }
})
const {
  sessionState,
  usage,
  flagConfirm,
  swarmEvents,
  swarmRunning,
  solveLocked,
  flowCompleted,
  suppressSolveGate,
  lastModels,
  lastFlagsRequired,
  swarmRoster,
  swarmFocus,
  swarmNavCursor,
  swarmNavArmed,
  solveFlowBusy,
  swarmStartedAt,
  swarmEndedAt,
  agentLineCounts,
  setupReady,
  setupMessage,
  setupInstalling,
  setupLogs,
} = rootSignals

class DaemonClient {
  // Direct callback registry for dialogs — SolidJS signals created outside
  // the TUI's reactive root don't reliably notify effects inside it (the
  // flags-ask dialog never re-ran its effect). Callbacks sidestep reactivity.
  private flagsAskCbs = new Set<(req: FlagsAskRequest) => void>()
  private flagsAskDismissCbs = new Set<(requestId: string) => void>()
  private solveFlowCbs = new Set<(req: SolveFlowRequest) => void>()

  /** Register a callback fired when a flags-ask dialog request arrives. */
  onFlagsAsk(cb: (req: FlagsAskRequest) => void): () => void {
    this.flagsAskCbs.add(cb)
    return () => this.flagsAskCbs.delete(cb)
  }

  /** Fired when the swarm cancels an in-flight flags-ask (dismiss the digits dialog). */
  onFlagsAskDismiss(cb: (requestId: string) => void): () => void {
    this.flagsAskDismissCbs.add(cb)
    return () => this.flagsAskDismissCbs.delete(cb)
  }

  /** Register a callback fired when the TUI solve-flow gate should run. */
  onSolveFlow(cb: (req: SolveFlowRequest) => void): () => void {
    this.solveFlowCbs.add(cb)
    return () => this.solveFlowCbs.delete(cb)
  }

  private sock: net.Socket | null = null
  private buf = ""
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void }>()
  /** Auto-reject timer after socket close — cleared on reconnect. */
  private flagConfirmCloseTimer: ReturnType<typeof setTimeout> | null = null
  private connectPromise: Promise<void> | null = null
  private closed = false
  /** OpenCode chat sessionID bound to this TUI window (null → daemon ``_default``). */
  private sessionId: string | null = null
  /** Home-screen load finishes before Session mounts — open the gate then. */
  private pendingSolveGate: SolveFlowRequest | null = null
  /** Invalidates /stop unlock timers so a newer stop/start cannot be cleared early. */
  private _swarmStopGeneration = 0
  /** Line count for event-buffer cap (strings are not kept). */
  private swarmLogCount = 0

  /**
   * Bind this client to an OpenCode session. Re-hellos when the id changes so
   * the daemon subscribes the peer under the correct session slot.
   */
    setSessionId(id: string | null | undefined): void {
    const next = id && String(id).trim() ? String(id).trim() : null
    if (next === this.sessionId) return
    const prev = this.sessionId
    // Decline pending y/n on the OLD session before rebind — request() would
    // otherwise stamp the new session and the swarm hangs until grace cancel.
    const pendingConfirm = this.flagConfirm[0]()
    if (prev !== next && pendingConfirm) {
      this.declinePendingFlagConfirm(prev)
    }
    this.sessionId = next
    // Always reset local swarm UI on chat switch — keeping a live session's
    // swarmRunning/events while rebinding the socket orphans /stop and the feed.
    if (prev !== next) {
      this.suppressSolveGate[1](false)
      this.solveLocked[1](false)
      this.flowCompleted[1](false)
      this.solveFlowBusy[1](false)
      this.lastModels[1]([])
      this.swarmRunning[1](false)
      this.clearSwarmEvents()
      this.sessionState[1]({})
    }
    if (this.sock && !this.sock.destroyed) {
      // Mid-install: keep the socket; re-hello with the new session so bake
      // progress (setup_log / setup_done) is not torn down as "interrupted".
      if (this.setupInstalling[0]()) {
        const helloId = Math.random().toString(16).slice(2, 12)
        this.writeRaw({
          v: 1,
          id: helloId,
          type: "hello",
          role: "tui",
          session: this.currentSession(),
        })
      } else {
        try {
          this.sock.destroy()
        } catch {
          /* ignore */
        }
        this.sock = null
        if (!this.closed) {
          void this.ensureConnected()
            .then(() => this.reconcileSwarmRunning())
            .catch(() => {})
        }
      }
    } else if (!this.closed) {
      void this.ensureConnected()
        .then(() => this.reconcileSwarmRunning())
        .catch(() => {})
    }
  }

  // Push-driven state signals (module-level — shared, reactive across components).
  sessionState = sessionState
  usage = usage
  flagConfirm = flagConfirm
  swarmEvents = swarmEvents
  swarmRunning = swarmRunning
  solveLocked = solveLocked
  flowCompleted = flowCompleted
  suppressSolveGate = suppressSolveGate
  lastModels = lastModels
  lastFlagsRequired = lastFlagsRequired
  swarmRoster = swarmRoster
  swarmFocus = swarmFocus
  swarmNavCursor = swarmNavCursor
  swarmNavArmed = swarmNavArmed
  solveFlowBusy = solveFlowBusy
  swarmStartedAt = swarmStartedAt
  swarmEndedAt = swarmEndedAt
  agentLineCounts = agentLineCounts
  setupReady = setupReady
  setupMessage = setupMessage
  setupInstalling = setupInstalling
  setupLogs = setupLogs

  /** Ensure we are connected + hello'd. Retries briefly on cold start. */
  async ensureConnected(): Promise<void> {
    if (this.sock && !this.sock.destroyed) return
    if (this.connectPromise) return this.connectPromise
    this.connectPromise = this.connect()
    try {
      await this.connectPromise
    } finally {
      this.connectPromise = null
    }
  }

  private connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      let attempts = 0
      let settled = false
      const tryOnce = () => {
        if (this.closed) return reject(new Error("client closed"))
        const s = createDaemonConnection()
        s.on("connect", () => {
          this.sock = s
          // Reconnected — do not auto-reject a confirm that may still be live.
          if (this.flagConfirmCloseTimer) {
            clearTimeout(this.flagConfirmCloseTimer)
            this.flagConfirmCloseTimer = null
          }
          // Wait for hello ack so the daemon has subscribed this socket before
          // we resolve (avoids missing early solve_flow_request pushes).
          const helloId = Math.random().toString(16).slice(2, 12)
          this.pending.set(helloId, {
            resolve: () => {
              if (!settled) {
                settled = true
                resolve()
              }
            },
            reject: (e) => {
              if (!settled) {
                settled = true
                reject(e)
              }
            },
          })
          this.writeRaw({
            v: 1,
            id: helloId,
            type: "hello",
            role: "tui",
            session: this.currentSession(),
          })
          setTimeout(() => {
            if (this.pending.has(helloId)) {
              this.pending.delete(helloId)
              // Socket is up even if ack is slow — still usable.
              if (!settled) {
                settled = true
                resolve()
              }
            }
          }, 2000)
        })
        s.setEncoding("utf8")
        s.on("data", (data: string) => this.onData(data))
        s.on("error", () => {
          attempts++
          if (attempts > 50) {
            reject(new Error("daemon socket connect failed"))
            return
          }
          setTimeout(tryOnce, attempts < 10 ? 25 : 100)
        })
        s.on("close", () => {
          if (this.sock === s) this.sock = null
          this.rejectAllPending(new Error("daemon socket closed"))
          // Mid-install: keep setupInstalling true and reconnect — baking
          // continues in the daemon; the Install dialog waits on setup_done.
          if (this.setupInstalling[0]()) {
            this.setupLogs[1]((prev) => [
              ...prev.slice(-79),
              "Daemon disconnected — reconnecting (install continues)…",
            ])
            if (!this.closed) {
              void this.ensureConnected().catch(() => {})
            }
          }
          // After reconnect grace the daemon cancels confirms with no listener —
          // drop a local bar that can never be answered. Cancel this timer if we
          // reconnect before it fires (grace is 10s; do not reject a live dialog).
          const stuck = this.flagConfirm[0]()
          if (stuck) {
            const rid = stuck.request_id
            if (this.flagConfirmCloseTimer) clearTimeout(this.flagConfirmCloseTimer)
            this.flagConfirmCloseTimer = setTimeout(() => {
              this.flagConfirmCloseTimer = null
              if (this.sock && !this.sock.destroyed) return
              const cur = this.flagConfirm[0]()
              if (!cur || cur.request_id !== rid) return
              void this.request("flag_confirm_answer", { request_id: rid, ok: false })
                .then(() => this.clearFlagConfirm(rid))
                .catch((e) => {
                  if (/no pending confirm/i.test(e instanceof Error ? e.message : String(e))) {
                    this.clearFlagConfirm(rid)
                  }
                })
            }, 11_000)
          }
          if (!this.closed) {
            // Auto-reconnect after a short backoff.
            setTimeout(() => {
              if (!this.closed) this.ensureConnected().catch(() => {})
            }, 400)
          }
        })
      }
      tryOnce()
    })
  }

  private currentSession(): string | null {
    return this.sessionId
  }

  private writeRaw(msg: Envelope): void {
    if (!this.sock || this.sock.destroyed) return
    this.sock.write(JSON.stringify(msg) + "\n")
  }

  private rejectAllPending(err: Error): void {
    const pending = [...this.pending.entries()]
    this.pending.clear()
    for (const [, p] of pending) {
      try {
        p.reject(err)
      } catch {
        /* ignore */
      }
    }
  }

  /** Bump generation used by /stop hung-cleanup unlock timers. */
  bumpSwarmStopGeneration(): number {
    this._swarmStopGeneration += 1
    return this._swarmStopGeneration
  }

  swarmStopGeneration(): number {
    return this._swarmStopGeneration
  }

  /** Send a request and await its response (correlated by id). */
  async request<T = unknown>(
    type: string,
    payload: Record<string, unknown> = {},
    opts?: { session?: string | null },
  ): Promise<T> {
    await this.ensureConnected()
    const id = Math.random().toString(16).slice(2, 12)
    const session =
      opts?.session !== undefined
        ? opts.session && String(opts.session).trim()
          ? String(opts.session).trim()
          : null
        : this.currentSession()
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => resolve(v as T),
        reject,
      })
      this.writeRaw({ v: 1, id, type, session, ...payload })
      // Safety timeout so a missing response never hangs the TUI.
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`daemon request '${type}' timed out`))
        }
      }, 30000)
    })
  }

  private onData(data: string): void {
    this.buf += data
    let nl: number
    while ((nl = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, nl)
      this.buf = this.buf.slice(nl + 1)
      if (!line.trim()) continue
      let msg: Envelope
      try {
        msg = JSON.parse(line)
      } catch {
        continue
      }
      this.dispatch(msg)
    }
  }

  /** Drop stale replay while flags→mode→models dialogs are open — never drop a live run. */
  private shouldDropSwarmPush(): boolean {
    return this.solveFlowBusy[0]() && !this.swarmRunning[0]()
  }

  private dispatch(msg: Envelope): void {
    const t = msg.type
    // Drop pushes stamped for another OpenCode session (multi-window daemon).
    if (!pushBelongsToSession(msg.session, this.sessionId)) {
      // Still resolve correlated request responses for our own requests.
      if (!(msg.id && this.pending.has(msg.id))) return
    }
    // Response to a pending request?
    if (msg.id && this.pending.has(msg.id)) {
      const p = this.pending.get(msg.id)!
      this.pending.delete(msg.id)
      if (msg.type === "error" || msg.ok === false) {
        p.reject(new Error(String(msg.error ?? "daemon error")))
      } else {
        p.resolve(msg)
      }
      return
    }
    switch (t) {
      case "session_update":
        this.sessionState[1]((msg.session_state as SessionState) ?? {})
        if (!(msg.session_state as SessionState)?.challenge_dir) {
          this.flowCompleted[1](false)
          this.solveLocked[1](false)
          this.suppressSolveGate[1](false)
        } else {
          this.markFlowCompletedIfDone()
          this.ensureFinishedRecapVisible()
        }
        break
      case "usage_update":
        this.usage[1]({
          tokens: Number(msg.tokens ?? 0),
          input: Number(msg.input ?? 0),
          output: Number(msg.output ?? 0),
          cache_read: Number(msg.cache_read ?? 0),
          cost_usd: msg.cost_usd === null || msg.cost_usd === undefined ? null : Number(msg.cost_usd),
        })
        break
      case "flag_confirm_request":
        this.flagConfirm[1]({ request_id: String(msg.request_id), flag: String(msg.flag ?? "") })
        break
      case "flag_confirm_dismiss":
        this.clearFlagConfirm(msg.request_id ? String(msg.request_id) : undefined)
        break
      case "flags_ask_dismiss": {
        const rid = msg.request_id ? String(msg.request_id) : ""
        for (const cb of this.flagsAskDismissCbs) cb(rid)
        break
      }
      case "flags_ask_request": {
        if (this.shouldDropSwarmPush()) break
        const faReq = {
          request_id: String(msg.request_id),
          default: Number(msg.default ?? 1),
          challenge: msg.challenge as string | undefined,
        }
        for (const cb of this.flagsAskCbs) cb(faReq)
        break
      }
      case "solve_flow_request": {
        const sfReq: SolveFlowRequest = {
          default_flags: Number(msg.default_flags ?? msg.default ?? 1),
          challenge: msg.challenge as string | undefined,
          preselected: Array.isArray(msg.preselected) ? (msg.preselected as string[]) : undefined,
          from_load: msg.from_load === true,
        }
        // Session callbacks decide whether a fresh load unsuppresses.
        for (const cb of this.solveFlowCbs) cb(sfReq)
        break
      }
      case "swarm_log":
        // During flags/mode/models gate, ignore reconnect replay / late old logs.
        if (this.shouldDropSwarmPush()) break
        this.appendSwarmLog(String(msg.text ?? ""))
        break
      case "setup_log": {
        const line = String(msg.text ?? "").trim()
        if (!line) break
        this.setupLogs[1]((prev) => [...prev.slice(-80), line])
        break
      }
      case "setup_done": {
        const ready = msg.ready === true
        this.setupReady[1](ready)
        this.setupMessage[1](String(msg.message ?? ""))
        this.setupInstalling[1](false)
        break
      }
      case "boot":
        if (this.shouldDropSwarmPush()) break
        this.swarmRunning[1](true)
        // A "Starting swarm" boot line begins a fresh run → reset the buffer.
        if (String(msg.text ?? "").startsWith("Starting swarm")) {
          this.swarmLogCount = 1
          this.swarmEvents[1](this.parseEvents([String(msg.text ?? "")]))
          this.agentLineCounts[1]({})
          this.swarmStartedAt[1](Date.now())
          this.swarmEndedAt[1](null)
          this.swarmFocus[1](null)
          this.swarmNavCursor[1](0)
          this.swarmNavArmed[1](false)
        } else {
          this.appendSwarmLog(String(msg.text ?? ""))
        }
        break
      case "swarm_roster": {
        if (this.shouldDropSwarmPush()) break
        const agents = Array.isArray(msg.agents)
          ? (msg.agents as unknown[]).map((a) => String(a)).filter(Boolean)
          : []
        const models = Array.isArray(msg.models)
          ? (msg.models as unknown[]).map((m) => String(m)).filter(Boolean)
          : []
        if (models.length) this.lastModels[1](models)
        const next = rosterFromDaemonPush(agents, this.lastModels[0]())
        if (next.length) this.setSwarmRoster(next)
        this.swarmFocus[1](null)
        this.swarmNavCursor[1](0)
        this.swarmNavArmed[1](false)
        break
      }
      case "swarm_exit":
        this._swarmStopGeneration += 1
        this.swarmRunning[1](false)
        this.flagConfirm[1](null)
        // Freeze elapsed; keep swarmStartedAt until clear so the final time stays visible.
        if (this.swarmEndedAt[0]() == null) this.swarmEndedAt[1](Date.now())
        if (!this.solveFlowBusy[0]()) {
          this.markFlowCompletedIfDone()
          this.solveLocked[1](false)
          // Finished attempt — do not reopen flags/mode/models until user acts.
          this.suppressSolveGate[1](true)
        }
        break
      case "swarm_adopted":
        if (this.shouldDropSwarmPush()) break
        this.swarmRunning[1](true)
        this.adoptStartedAt(msg.started_at)
        break
      case "replay_done": {
        if (this.shouldDropSwarmPush()) break
        // Reconcile running state after reconnect. If the log already has a
        // terminal outcome (quota / CORRECT) but the process is still in
        // sandbox cleanup, keep the UI unlocked and do not reopen the gate.
        const running = Boolean(msg.running)
        if (!running) {
          const terminal =
            this.flowCompleted[0]() ||
            hasTerminalSolveOutcome(this.swarmEvents[0](), this.isMultiAgent())
          this.swarmRunning[1](false)
          this.solveLocked[1](false)
          if (terminal) {
            // Finished solve the operator is still reading — never wipe CORRECT /
            // How-the-flag-was-found just because the daemon is idle again.
            this.suppressSolveGate[1](true)
            this.freezeElapsedIfTerminal()
            break
          }
          // True cold idle: drop previous-run agent grid so a fresh TUI never
          // flashes stale boxes before the flags dialog. Do not suppress just
          // because a challenge is loaded — that is the normal pre-gate state
          // (LLM load → ask_flags, or reconnect mid-setup).
          this.clearSwarmEvents()
          break
        }
        const terminal = hasTerminalSolveOutcome(this.swarmEvents[0](), this.isMultiAgent())
        // Process still alive → keep swarmRunning so Esc/stop work; unlock chat if terminal.
        this.swarmRunning[1](true)
        this.adoptStartedAt(msg.started_at)
        if (terminal) {
          this.solveLocked[1](false)
          this.suppressSolveGate[1](true)
          this.freezeElapsedIfTerminal()
        }
        break
      }
      default:
        // hello ack / other responses without a pending request: ignore.
        break
    }
  }

  /**
   * Adopt a daemon-reported swarm start time so elapsed survives reconnects.
   * A locally known start always wins (it is the precise one).
   */
  private adoptStartedAt(raw: unknown): void {
    if (this.swarmStartedAt[0]() != null) return
    const ms = typeof raw === "number" && Number.isFinite(raw) && raw > 0 ? raw : Date.now()
    this.swarmStartedAt[1](ms)
    // Do not un-freeze a terminal Hold/Writeup clock when adopting start on reconnect.
    const terminal =
      this.flowCompleted[0]() ||
      hasTerminalSolveOutcome(this.swarmEvents[0](), this.isMultiAgent())
    if (!terminal) this.swarmEndedAt[1](null)
  }

  /** Freeze solve elapsed once a terminal outcome exists (Hold must not keep ticking). */
  private freezeElapsedIfTerminal(events?: ArtemisEvent[]): void {
    if (this.swarmEndedAt[0]() != null) return
    if (this.swarmStartedAt[0]() == null) return
    const list = events ?? this.swarmEvents[0]()
    const terminal =
      this.flowCompleted[0]() ||
      hasTerminalSolveOutcome(list, this.isMultiAgent())
    if (!terminal) return
    this.swarmEndedAt[1](Date.now())
  }

  /**
   * Answer a flag-confirm. Keep the bar visible until the daemon acks.
   *
   * `reason` is the operator's optional one-liner for a rejection; it is relayed
   * to the solver so it can tell a wrong flag from a wrong technique.
   */
  async answerFlagConfirm(request_id: string, ok: boolean, reason = ""): Promise<unknown> {
    const pending = this.flagConfirm[0]()
    try {
      const res = await this.request("flag_confirm_answer", { request_id, ok, reason })
      this.clearFlagConfirm(request_id)
      return res
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      // Daemon already dropped this confirm (reconnect past grace) — do not
      // resurrect a stuck y/n bar that can never complete.
      if (/no pending confirm/i.test(msg)) {
        this.clearFlagConfirm(request_id)
        return { ok: false, error: msg }
      }
      // Restore so the operator can retry if the socket failed mid-flight.
      if (pending && this.flagConfirm[0]() == null) this.flagConfirm[1](pending)
      throw e
    }
  }

  /** Drop the bar only if it still shows ``request_id`` (a newer ask must survive). */
  private clearFlagConfirm(request_id?: string): void {
    this.flagConfirm[1]((cur) => (cur && request_id && cur.request_id !== request_id ? cur : null))
  }

  /**
   * Decline a pending flag confirm so the swarm side stops waiting.
   * Optional ``session`` targets a prior slot (session switch).
   */
  declinePendingFlagConfirm(session?: string | null): void {
    const pending = this.flagConfirm[0]()
    if (!pending) return
    this.clearFlagConfirm(pending.request_id)
    void this.request(
      "flag_confirm_answer",
      { request_id: pending.request_id, ok: false },
      session !== undefined ? { session } : undefined,
    ).catch(() => {})
  }

  /** Answer a flags-ask dialog. */
  answerFlagsAsk(request_id: string, n: number): Promise<unknown> {
    return this.request("flags_ask_answer", { request_id, n })
  }

  /** Start swarm with models chosen by the TUI solve-flow gate. */
  async startSwarm(opts: {
    models: string[]
    flags_required: number
    force?: boolean
    auto_confirm?: boolean
  }): Promise<unknown> {
    // Fresh run — drop prior events so old think lines cannot invent agent boxes.
    this._swarmStopGeneration += 1
    this.swarmLogCount = 0
    this.swarmEvents[1]([])
    this.solveLocked[1](true)
    this.swarmRunning[1](true)
    this.flowCompleted[1](false)
    this.suppressSolveGate[1](false)
    this.lastModels[1](opts.models)
    this.lastFlagsRequired[1](opts.flags_required)
    this.swarmRoster[1](rosterFromSpecs(opts.models))
    this.swarmFocus[1](null)
    this.swarmNavCursor[1](0)
    this.swarmNavArmed[1](false)
    this.agentLineCounts[1]({})
    this.swarmStartedAt[1](Date.now())
    this.swarmEndedAt[1](null)
    this.usage[1](null)
    try {
      const res = await this.request("swarm_start", {
        models: opts.models,
        flags_required: opts.flags_required,
        force: opts.force !== false,
        auto_confirm: opts.auto_confirm === true,
      })
      this.scheduleSwarmReplayIfEmpty()
      return res
    } catch (e) {
      // RPC timeout/error can race a successful spawn — only clear UI if daemon idle.
      const stillRunning = await this.reconcileSwarmRunning()
      if (!stillRunning) {
        this.swarmRunning[1](false)
        this.solveLocked[1](false)
      }
      throw e
    }
  }

  /** Mid-solve / post-CORRECT hold note for sandbox solvers (not OpenCode chat). */
  async sendOperatorMessage(
    text: string,
    opts?: { delivery?: "steer" | "queue"; target?: string; noFanout?: boolean },
  ): Promise<unknown> {
    return this.request("swarm_operator_message", {
      text,
      delivery: opts?.delivery ?? "steer",
      ...(opts?.target ? { target: opts.target } : {}),
      ...(opts?.noFanout ? { no_fanout: true } : {}),
    })
  }

  async refreshSetupStatus(): Promise<boolean> {
    try {
      await this.ensureConnected()
      const res = (await this.request<{
        ready?: boolean
        message?: string
      }>("setup_status", {})) as { ready?: boolean; message?: string }
      const ready = res?.ready === true
      this.setupReady[1](ready)
      this.setupMessage[1](String(res?.message ?? ""))
      return ready
    } catch (e) {
      // Transient probe failure — keep prior ready state so a reconnect blip
      // does not re-open the Install gate when packs are already baked.
      this.setupMessage[1](e instanceof Error ? e.message : "Cannot reach Artemis daemon")
      return this.setupReady[0]() === true
    }
  }

  async startSetupInstall(opts?: { skipWarmRuntime?: boolean }): Promise<void> {
    const already = this.setupInstalling[0]() === true
    this.setupInstalling[1](true)
    // Coalesced second call must not wipe logs from an in-flight bake.
    if (!already) this.setupLogs[1]([])
    await this.ensureConnected()
    await this.request("setup_install", {
      skip_warm_runtime: opts?.skipWarmRuntime !== false,
    })
  }

  /** Recover missed boot/log pushes (Windows race / dialog timing). */
  scheduleSwarmReplayIfEmpty(): void {
    queueMicrotask(() => {
      setTimeout(() => {
        if (!this.swarmRunning[0]()) return
        const ev = this.swarmEvents[0]()
        const hasBoot = ev.some((e) => e.kind === "boot")
        if (hasBoot || hasTerminalSolveOutcome(ev, this.isMultiAgent())) return
        void this.request("swarm_replay", {}).catch(() => {})
      }, 250)
    })
  }

  /** Ask daemon whether this session's swarm is still live. */
  async reconcileSwarmRunning(): Promise<boolean> {
    try {
      const res = (await this.request<{ swarm_running?: boolean }>("status", {})) as {
        swarm_running?: boolean
      }
      const running = Boolean(res?.swarm_running)
      // Must clear as well as set — otherwise a dead process (missed swarm_exit
      // after hung writeup) leaves the footer on Stopping forever.
      this.swarmRunning[1](running)
      if (!running) {
        if (this.swarmEndedAt[0]() == null && this.swarmStartedAt[0]() != null) {
          this.swarmEndedAt[1](Date.now())
        }
        this.markFlowCompletedIfDone()
        this.ensureFinishedRecapVisible()
      }
      return running
    } catch {
      return this.swarmRunning[0]()
    }
  }

  setSolveLocked(locked: boolean): void {
    this.solveLocked[1](locked)
  }

  setLastModels(models: string[]): void {
    this.lastModels[1](models)
  }

  setLastFlagsRequired(n: number): void {
    this.lastFlagsRequired[1](n)
  }

  setFlowCompleted(done: boolean): void {
    this.flowCompleted[1](done)
  }

  setSuppressSolveGate(suppress: boolean): void {
    this.suppressSolveGate[1](suppress)
  }

  setPendingSolveGate(req: SolveFlowRequest): void {
    this.pendingSolveGate = req
  }

  takePendingSolveGate(): SolveFlowRequest | null {
    const req = this.pendingSolveGate
    this.pendingSolveGate = null
    return req
  }

  clearSwarmEvents(): void {
    this.swarmLogCount = 0
    this.swarmEvents[1]([])
    this.swarmRoster[1]([])
    this.swarmFocus[1](null)
    this.swarmNavCursor[1](0)
    this.swarmNavArmed[1](false)
    this.agentLineCounts[1]({})
    this.swarmStartedAt[1](null)
    this.swarmEndedAt[1](null)
    this.usage[1](null)
  }

  setSwarmFocus(agent: string | null): void {
    this.swarmFocus[1](agent)
    if (agent == null) this.swarmNavCursor[1](0)
  }

  setSwarmNavCursor(index: number): void {
    this.swarmNavCursor[1](Math.max(0, index))
  }

  setSwarmNavArmed(armed: boolean): void {
    this.swarmNavArmed[1](armed)
  }

  toggleSwarmNavArmed(): void {
    this.swarmNavArmed[1]((v) => !v)
  }

  setSolveFlowBusy(busy: boolean): void {
    this.solveFlowBusy[1](busy)
  }

  /** Apply session snapshot from a daemon load/status response (before push arrives). */
  applySessionState(state: SessionState | null | undefined): void {
    this.sessionState[1](state ?? {})
    if (!state?.challenge_dir) {
      this.flowCompleted[1](false)
      this.solveLocked[1](false)
      this.suppressSolveGate[1](false)
    } else {
      this.markFlowCompletedIfDone()
      this.ensureFinishedRecapVisible()
    }
  }

  /**
   * Load a challenge via daemon (path and/or pasted prompt).
   * Used by the TUI paste interceptor so any chat provider can start the flow.
   */
  async loadChallenge(args: Record<string, unknown>): Promise<{
    text: string
    session_state: SessionState
  }> {
    const res = await this.request<{
      text?: string
      session_state?: SessionState
      ok?: boolean
      error?: string
    }>("load", args)
    if (res.ok === false && res.error) {
      throw new Error(String(res.error))
    }
    const text = String(res.text ?? "")
    const session_state = (res.session_state ?? {}) as SessionState
    // Failed loads must not re-apply stale challenge_dir / recap state.
    if (!/^ERROR/i.test(text.trim())) {
      this.applySessionState(session_state)
    }
    return { text, session_state }
  }

  setSwarmRoster(agents: string[]): void {
    this.swarmRoster[1](agents)
    const focus = this.swarmFocus[0]()
    if (focus == null) {
      this.swarmNavCursor[1](0)
      return
    }
    const idx = agents.indexOf(focus)
    if (idx < 0) {
      this.swarmFocus[1](null)
      this.swarmNavCursor[1](0)
    } else {
      this.swarmNavCursor[1](idx + 1)
    }
  }

  private markFlowCompletedIfDone(): void {
    const st = this.sessionState[0]()
    const required = Number(st.flags_required ?? this.lastFlagsRequired[0]() ?? 1)
    const accepted = (st.accepted_flags ?? []).length
    if (accepted >= required && required > 0) {
      this.flowCompleted[1](true)
      this.ensureFinishedRecapVisible()
    }
  }

  /**
   * When sidebar already shows n/n flags but the main feed lost CORRECT/summary
   * (writeup hang, reconnect clear, log cap), inject a sticky recap so the
   * operator still sees How / flags on the main page.
   */
  private ensureFinishedRecapVisible(): void {
    // Live swarm / Hold still streaming — wait for real CORRECT/Solved by from
    // the log (session_update can arrive before replay and would duplicate).
    if (this.swarmRunning[0]()) return
    const st = this.sessionState[0]()
    const required = Number(st.flags_required ?? this.lastFlagsRequired[0]() ?? 1)
    const accepted = [...(st.accepted_flags ?? [])].filter(Boolean)
    if (accepted.length < required || required <= 0) return

    this.swarmEvents[1]((prev) => {
      const hasCorrect = prev.some(
        (ev) =>
          ev.kind === "outcome" &&
          ev.level === "success" &&
          /CORRECT|Challenge complete/i.test(ev.text),
      )
      // Interim "Writing recap…" is a placeholder — do not treat it as Solved by.
      const hasSolvedBy = prev.some(
        (ev) => ev.kind === "summary" && /^Solved by /i.test(String(ev.text || "")),
      )
      if (hasCorrect && hasSolvedBy) return prev

      const extra: ArtemisEvent[] = []
      if (!hasCorrect) {
        extra.push({
          kind: "outcome",
          level: "success",
          text:
            `CORRECT — accepted all ${accepted.length} flags: ${accepted.join(" | ")}. ` +
            "Challenge complete for this run.",
        })
      }
      if (!hasSolvedBy) {
        const roster = this.swarmRoster[0]()
        const viaMatch = [...prev, ...extra]
          .filter((ev) => ev.kind === "outcome")
          .filter((ev) =>
            /CORRECT|Challenge complete|FLAG FOUND/i.test(String(ev.text || "")),
          )
          .map((ev) => String(ev.text || "").match(/\bvia\s+([^\s,;:()]+)/i))
          .filter(Boolean)
          .pop()
        const rawVia = viaMatch?.[1]?.replace(/\.$/, "")
        const models = this.lastModels[0]()
        const soleModel =
          models.length === 1 ? agentKeyFromSpec(String(models[0])) : undefined
        const who =
          (rawVia ? agentKeyFromSpec(rawVia) : undefined) ||
          (roster.length === 1 ? roster[0] : undefined) ||
          soleModel
        extra.push({
          kind: "summary",
          text: who
            ? `Solved by ${who}`
            : `Solved · ${accepted.length}/${required} flags`,
        })
      }
      return extra.length ? [...prev, ...extra] : prev
    })
    this.freezeElapsedIfTerminal()
  }

  /** Append a swarm log line, capping the buffer to bound memory.
   * Parses the new line incrementally and appends to swarmEvents (avoids
   * O(n²) re-parse of the whole buffer on every line). */
  private appendSwarmLog(line: string): void {
    let dropped = 0
    if (this.swarmLogCount >= SWARM_LOG_CAP) {
      dropped = this.swarmLogCount - SWARM_LOG_CAP + 1
      this.swarmLogCount = SWARM_LOG_CAP
    } else {
      this.swarmLogCount += 1
    }
    this.swarmEvents[1]((prev) => {
      // Same ingest as parseArtemisEvents (qa_sep + summary expand).
      const incoming = eventsFromLogLine(line)
      let base = prev
      if (dropped > 0) {
        const isSticky = (e: ArtemisEvent): boolean =>
          e.kind === "summary" ||
          (e.kind === "operator" && e.delivery === "queue") ||
          (e.kind === "outcome" &&
            (e.level === "success" || /CORRECT|Challenge complete/i.test(e.text))) ||
          (e.kind === "status" && /^Hold\b/i.test(e.text))

        // Evict non-sticky first so sticky Hold/CORRECT cannot pin the buffer
        // forever when they sit at index 0.
        const next = [...prev]
        let need = Math.max(1, Math.min(dropped, next.length))
        while (need > 0 && next.length > 0) {
          const idx = next.findIndex((e) => !isSticky(e))
          if (idx >= 0) {
            next.splice(idx, 1)
            need--
            continue
          }
          // All sticky — prefer dropping older Hold banners (esp. when a Hold
          // released exists), then Hold released, then oldest sticky.
          // Never soft-evict pending Queue crumbs — OperatorQueueBar would
          // clear while the inbox note is still undrained.
          const holdIdx = next.findIndex(
            (e) =>
              e.kind === "status" &&
              /^Hold\b/i.test(e.text) &&
              !/^Hold released\b/i.test(e.text),
          )
          const holdReleasedIdx = next.findIndex(
            (e) => e.kind === "status" && /^Hold released\b/i.test(e.text),
          )
          const holdCount = next.filter(
            (e) =>
              e.kind === "status" &&
              /^Hold\b/i.test(e.text) &&
              !/^Hold released\b/i.test(e.text),
          ).length
          if (holdIdx >= 0 && (holdReleasedIdx >= 0 || holdCount > 1)) {
            next.splice(holdIdx, 1)
            need--
            continue
          }
          if (holdReleasedIdx >= 0) {
            next.splice(holdReleasedIdx, 1)
            need--
            continue
          }
          // Prefer dropping non-critical sticky before CORRECT / Solved by /
          // Hold / undrained Queue (those keep terminalDone + Hold routing honest).
          const isCriticalSticky = (e: (typeof next)[number]) => {
            if (e.kind === "operator" && e.delivery === "queue") return true
            if (
              e.kind === "status" &&
              (/^Hold\b/i.test(e.text) || /^Hold released\b/i.test(e.text))
            ) {
              return true
            }
            if (
              e.kind === "outcome" &&
              e.level === "success" &&
              /CORRECT|Challenge complete/i.test(e.text)
            ) {
              return true
            }
            if (e.kind === "summary" && /^Solved by /i.test(String(e.text || ""))) {
              return true
            }
            return false
          }
          const droppableSticky = next.findIndex((e) => !isCriticalSticky(e))
          if (droppableSticky >= 0) {
            next.splice(droppableSticky, 1)
            need--
            continue
          }
          next.shift()
          need--
        }
        base = next
      }
      if (incoming.length === 0) return base
      // Collect agent bumps here; apply once after this setter (avoids nested
      // Solid updates per line that thrash the swarm UI).
      const bumps: Record<string, number> = {}
      for (const piece of incoming) {
        const agent =
          piece.kind !== "boot" &&
          "agent" in piece &&
          typeof (piece as { agent?: string }).agent === "string"
            ? (piece as { agent: string }).agent
            : null
        if (agent) bumps[agent] = (bumps[agent] ?? 0) + 1
      }
      // Always replace the coalesced tail — never append a slice when merge
      // shortened the tail (that path duplicated/dropped events and made
      // event counts jump down).
      const tailLen = Math.min(base.length, 64)
      const head = base.slice(0, base.length - tailLen)
      const tail = base.slice(base.length - tailLen)
      // Seed dedupe with the unmerged head so CORRECT outside the 64-event
      // window still suppresses later FLAG FOUND / Confirmed dupes.
      const merged = dedupeEvents(coalesceEvents([...tail, ...incoming]), head)
      const nextEvents = [...head, ...merged]
      if (Object.keys(bumps).length > 0) {
        queueMicrotask(() => {
          this.agentLineCounts[1]((counts) => {
            const next = { ...counts }
            for (const [agent, n] of Object.entries(bumps)) {
              next[agent] = (next[agent] ?? 0) + n
            }
            return next
          })
        })
      }
      if (hasTerminalSolveOutcome(nextEvents, this.isMultiAgent())) {
        // Freeze with nextEvents — swarmEvents[0] is still the previous buffer
        // inside this Solid setter.
        this.freezeElapsedIfTerminal(nextEvents)
        queueMicrotask(() => {
          if (this.solveFlowBusy[0]()) return
          this.freezeElapsedIfTerminal()
          // Unlock chat / Solving UI — keep swarmRunning until swarm_exit so
          // Esc / /stop still work while siblings tear down.
          this.solveLocked[1](false)
          this.suppressSolveGate[1](true)
          this.declinePendingFlagConfirm()
          if (
            incoming.some(
              (e) =>
                e.kind === "outcome" &&
                e.level === "success" &&
                /CORRECT|Challenge complete/i.test(e.text),
            )
          ) {
            this.flowCompleted[1](true)
          }
          this.markFlowCompletedIfDone()
          this.ensureFinishedRecapVisible()
          // Quota/CORRECT: jump back to main so chat unlocks and ↑↓ scroll works.
          if (this.isMultiAgent()) {
            this.swarmFocus[1](null)
            this.swarmNavCursor[1](0)
            this.swarmNavArmed[1](false)
          }
        })
      }
      return nextEvents
    })
  }

  private isMultiAgent(): boolean {
    return this.swarmRoster[0]().length > 1 || this.lastModels[0]().length > 1
  }

  /** Parse a set of lines into coalesced/deduped events (used on reset/replay). */
  private parseEvents(lines: string[]): ArtemisEvent[] {
    const out: ArtemisEvent[] = []
    for (const line of lines) {
      out.push(...eventsFromLogLine(line))
    }
    return dedupeEvents(coalesceEvents(out))
  }

  /**
   * TUI process exit: stop the live swarm (and sandboxes via daemon) then
   * disconnect. Idempotent. Best-effort — never block exit for long.
   */
  async shutdown(): Promise<void> {
    if (this.closed) return
    try {
      if (this.sock && !this.sock.destroyed) {
        await Promise.race([
          this.request("swarm_stop", {}).then(() => undefined),
          new Promise<void>((resolve) => setTimeout(resolve, 4000)),
        ]).catch(() => {})
      }
    } finally {
      this.closed = true
      this.swarmRunning[1](false)
      this.solveLocked[1](false)
      try {
        this.sock?.destroy()
      } catch {
        /* ignore */
      }
      this.sock = null
    }
  }
}

/** Singleton daemon client for the TUI process. */
export const daemon = new DaemonClient()
