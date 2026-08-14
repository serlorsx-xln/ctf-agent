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
import { parseLine, coalesceEvents, dedupeEvents, expandSummaryLine, hasTerminalSolveOutcome, type ArtemisEvent } from "../util/artemis-live-log"
import { rosterFromSpecs } from "../util/artemis-swarm-agents"

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
export type SolveFlowRequest = { default_flags: number; challenge?: string; preselected?: string[] }

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
  const flagsAsk = createSignal<FlagsAskRequest | null>(null)
  const swarmLog = createSignal<string[]>([])
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
  /** Wall-clock ms when the current swarm run exited (freeze elapsed). */
  const swarmEndedAt = createSignal<number | null>(null)
  /** Monotonic per-agent parsed-line counts (never decreases during a run). */
  const agentLineCounts = createSignal<Record<string, number>>({})
  return {
    sessionState,
    usage,
    flagConfirm,
    flagsAsk,
    swarmLog,
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
  }
})
const {
  sessionState,
  usage,
  flagConfirm,
  flagsAsk,
  swarmLog,
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
} = rootSignals

class DaemonClient {
  // Direct callback registry for dialogs — SolidJS signals created outside
  // the TUI's reactive root don't reliably notify effects inside it (the
  // flags-ask dialog never re-ran its effect). Callbacks sidestep reactivity.
  private flagsAskCbs = new Set<(req: FlagsAskRequest) => void>()
  private solveFlowCbs = new Set<(req: SolveFlowRequest) => void>()

  /** Register a callback fired when a flags-ask dialog request arrives. */
  onFlagsAsk(cb: (req: FlagsAskRequest) => void): () => void {
    this.flagsAskCbs.add(cb)
    return () => this.flagsAskCbs.delete(cb)
  }

  /** Register a callback fired when the TUI solve-flow gate should run. */
  onSolveFlow(cb: (req: SolveFlowRequest) => void): () => void {
    this.solveFlowCbs.add(cb)
    return () => this.solveFlowCbs.delete(cb)
  }

  private sock: net.Socket | null = null
  private buf = ""
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void }>()
  private connectPromise: Promise<void> | null = null
  private closed = false
  /** OpenCode chat sessionID bound to this TUI window (null → daemon ``_default``). */
  private sessionId: string | null = null
  /** Invalidates /stop unlock timers so a newer stop/start cannot be cleared early. */
  private _swarmStopGeneration = 0

  /**
   * Bind this client to an OpenCode session. Re-hellos when the id changes so
   * the daemon subscribes the peer under the correct session slot.
   */
  setSessionId(id: string | null | undefined): void {
    const next = id && String(id).trim() ? String(id).trim() : null
    if (next === this.sessionId) return
    this.sessionId = next
    if (this.sock && !this.sock.destroyed) {
      try {
        this.sock.destroy()
      } catch {
        /* ignore */
      }
      this.sock = null
    }
    if (!this.closed) {
      void this.ensureConnected().catch(() => {})
    }
  }

  currentSessionId(): string | null {
    return this.sessionId
  }

  // Push-driven state signals (module-level — shared, reactive across components).
  sessionState = sessionState
  usage = usage
  flagConfirm = flagConfirm
  flagsAsk = flagsAsk
  swarmLog = swarmLog
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
          setTimeout(tryOnce, 100)
        })
        s.on("close", () => {
          if (this.sock === s) this.sock = null
          this.rejectAllPending(new Error("daemon socket closed"))
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
  async request<T = unknown>(type: string, payload: Record<string, unknown> = {}): Promise<T> {
    await this.ensureConnected()
    const id = Math.random().toString(16).slice(2, 12)
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => resolve(v as T),
        reject,
      })
      this.writeRaw({ v: 1, id, type, session: this.currentSession(), ...payload })
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
      case "flags_ask_request": {
        if (this.shouldDropSwarmPush()) break
        const faReq = {
          request_id: String(msg.request_id),
          default: Number(msg.default ?? 1),
          challenge: msg.challenge as string | undefined,
        }
        this.flagsAsk[1](faReq)
        for (const cb of this.flagsAskCbs) cb(faReq)
        break
      }
      case "solve_flow_request": {
        const sfReq: SolveFlowRequest = {
          default_flags: Number(msg.default_flags ?? msg.default ?? 1),
          challenge: msg.challenge as string | undefined,
          preselected: Array.isArray(msg.preselected) ? (msg.preselected as string[]) : undefined,
        }
        for (const cb of this.solveFlowCbs) cb(sfReq)
        break
      }
      case "swarm_log":
        // During flags/mode/models gate, ignore reconnect replay / late old logs.
        if (this.shouldDropSwarmPush()) break
        this.appendSwarmLog(String(msg.text ?? ""))
        break
      case "boot":
        if (this.shouldDropSwarmPush()) break
        this.swarmRunning[1](true)
        // A "Starting swarm" boot line begins a fresh run → reset the buffer.
        if (String(msg.text ?? "").startsWith("Starting swarm")) {
          this.swarmLog[1]([String(msg.text ?? "")])
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
        this.setSwarmRoster(agents)
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
            break
          }
          // True cold idle: drop previous-run agent grid so a fresh TUI never
          // flashes stale boxes before the flags dialog.
          this.clearSwarmEvents()
          const hasChallenge = Boolean(this.sessionState[0]()?.challenge_dir)
          if (hasChallenge) this.suppressSolveGate[1](true)
          break
        }
        const terminal = hasTerminalSolveOutcome(this.swarmEvents[0](), this.isMultiAgent())
        // Process still alive → keep swarmRunning so Esc/stop work; unlock chat if terminal.
        this.swarmRunning[1](true)
        this.adoptStartedAt(msg.started_at)
        if (terminal) {
          this.solveLocked[1](false)
          this.suppressSolveGate[1](true)
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
    this.swarmEndedAt[1](null)
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
      // Restore so the operator can retry if the socket failed.
      if (pending && this.flagConfirm[0]() == null) this.flagConfirm[1](pending)
      throw e
    }
  }

  /** Drop the bar only if it still shows ``request_id`` (a newer ask must survive). */
  private clearFlagConfirm(request_id?: string): void {
    this.flagConfirm[1]((cur) => (cur && request_id && cur.request_id !== request_id ? cur : null))
  }

  /**
   * A pending confirm is unanswerable (run reached a terminal outcome).
   * Decline it so the swarm side stops waiting instead of hanging on a future.
   */
  private declinePendingFlagConfirm(): void {
    const pending = this.flagConfirm[0]()
    if (!pending) return
    this.clearFlagConfirm(pending.request_id)
    void this.request("flag_confirm_answer", { request_id: pending.request_id, ok: false }).catch(
      () => {},
    )
  }

  /** Answer a flags-ask dialog. Clears the pending signal. */
  answerFlagsAsk(request_id: string, n: number): Promise<unknown> {
    this.flagsAsk[1](null)
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
    this.swarmLog[1]([])
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

  /** Recover missed boot/log pushes (Windows race / dialog timing). */
  scheduleSwarmReplayIfEmpty(): void {
    queueMicrotask(() => {
      setTimeout(() => {
        if (!this.swarmRunning[0]()) return
        const ev = this.swarmEvents[0]()
        const hasBoot = ev.some((e) => e.kind === "boot")
        if (hasBoot || hasTerminalSolveOutcome(ev, this.isMultiAgent())) return
        void this.request("swarm_replay", {}).catch(() => {})
      }, 700)
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

  clearSwarmEvents(): void {
    this.swarmLog[1]([])
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
    this.applySessionState(session_state)
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
      const hasSummary = prev.some((ev) => ev.kind === "summary")
      if (hasCorrect && hasSummary) return prev

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
      if (!hasSummary) {
        extra.push({ kind: "summary", text: `Solved · ${accepted.length}/${required} flags` })
      }
      return extra.length ? [...prev, ...extra] : prev
    })
  }

  /** Append a swarm log line, capping the buffer to bound memory.
   * Parses the new line incrementally and appends to swarmEvents (avoids
   * O(n²) re-parse of the whole buffer on every line). */
  private appendSwarmLog(line: string): void {
    let dropped = 0
    this.swarmLog[1]((prev) => {
      if (prev.length >= SWARM_LOG_CAP) {
        dropped = prev.length - SWARM_LOG_CAP + 1
        return [...prev.slice(dropped), line]
      }
      return [...prev, line]
    })
    this.swarmEvents[1]((prev) => {
      const ev = parseLine(line)
      let base = prev
      if (dropped > 0) {
        const sliced = prev.slice(0, Math.min(prev.length, dropped))
        const sticky = sliced.filter(
          (e) =>
            e.kind === "summary" ||
            (e.kind === "outcome" &&
              (e.level === "success" || /CORRECT|Challenge complete/i.test(e.text))),
        )
        base = [...sticky, ...prev.slice(Math.min(prev.length, dropped))]
      }
      if (!ev) return base
      // Fan out jammed summary lines (1. a 2. b / **bold**) at ingest so live
      // streams match parseArtemisEvents / backend expand_summary_line.
      const incoming: ArtemisEvent[] =
        ev.kind === "summary"
          ? expandSummaryLine(ev.text).map((text) => ({ kind: "summary" as const, text }))
          : [ev]
      if (incoming.length === 0) return base
      // Monotonic activity counter per agent (coalesce must not shrink the UI
      // count). Boot lines name the owning container but are not agent work.
      for (const piece of incoming) {
        const agent =
          piece.kind !== "boot" &&
          "agent" in piece &&
          typeof (piece as { agent?: string }).agent === "string"
            ? (piece as { agent: string }).agent
            : null
        if (agent) {
          this.agentLineCounts[1]((counts) => ({
            ...counts,
            [agent]: (counts[agent] ?? 0) + 1,
          }))
        }
      }
      // Always replace the coalesced tail — never append a slice when merge
      // shortened the tail (that path duplicated/dropped events and made
      // event counts jump down).
      const tailLen = Math.min(base.length, 64)
      const head = base.slice(0, base.length - tailLen)
      const tail = base.slice(base.length - tailLen)
      const merged = dedupeEvents(coalesceEvents([...tail, ...incoming]))
      const nextEvents = [...head, ...merged]
      if (hasTerminalSolveOutcome(nextEvents, this.isMultiAgent())) {
        queueMicrotask(() => {
          if (this.solveFlowBusy[0]()) return
          // Unlock chat / Solving UI — keep swarmRunning until swarm_exit so
          // Esc / /stop still work while siblings tear down.
          this.solveLocked[1](false)
          this.suppressSolveGate[1](true)
          this.declinePendingFlagConfirm()
          if (
            ev.kind === "outcome" &&
            ev.level === "success" &&
            /CORRECT|Challenge complete/i.test(ev.text)
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
      const ev = parseLine(line)
      if (ev) out.push(ev)
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
