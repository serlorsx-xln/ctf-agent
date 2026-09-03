import { daemon } from "../artemis/client"

/**
 * Shared "is there work / residue?" checks for every path that clears a solve
 * (prompt submit, /restart, /new, /clear). Keeping them here stops the three
 * call sites from drifting into asking different questions.
 */

/** Whether the Solve card should render (hide idle "No solver activity"). */
export function swarmHasVisibleSolveUi(opts: {
  running: boolean
  eventCount: number
  startedAt: number | null
  agentStarted?: boolean
  showGrid?: boolean
}): boolean {
  if (opts.running || opts.showGrid || opts.agentStarted) return true
  if (opts.eventCount > 0) return true
  if (opts.startedAt != null) return true
  return false
}

/**
 * The flags → mode → models gate owns the screen right now. Commands must not
 * clear it out from under the operator. (The command palette is itself a
 * dialog, so a plain "any dialog open" check would reject every palette run.)
 */
export function solveGateOpen(): boolean {
  return daemon.solveFlowBusy[0]()
}

/** Block commands that would replace the flags→models dialogs. */
export function warnIfSolveGateOpen(show: (opts: { message: string; variant: "warning" }) => void): boolean {
  if (!solveGateOpen()) return false
  show({ message: "Finish or cancel the current dialog first", variant: "warning" })
  return true
}

/** Swarm or solve still in progress — clearing must stop it first. */
export function hasActiveSolveWork(): boolean {
  if (daemon.swarmRunning[0]()) return true
  if (daemon.solveLocked[0]() && !daemon.solveFlowBusy[0]()) return true
  return false
}

/** Daemon-side state from a prior/current challenge that would stack. */
export function hasDaemonArtemisResidue(): boolean {
  if (hasActiveSolveWork()) return true
  if (daemon.flowCompleted[0]()) return true
  if (daemon.suppressSolveGate[0]()) return true
  if (daemon.swarmEvents[0]().length > 0) return true
  if (daemon.swarmRoster[0]().length > 0) return true
  if (daemon.lastModels[0]().length > 0) return true
  const st = daemon.sessionState[0]()
  if (st?.challenge_dir || st?.challenge_name) return true
  return false
}

/** Options for the confirm dialog that matches the current state. */
export function confirmRestartOpts(): { active: boolean; completed: boolean } {
  const active = hasActiveSolveWork()
  return { active, completed: !active && daemon.flowCompleted[0]() }
}

/** Stop any live swarm and reset daemon + swarm UI state. Never throws. */
export async function stopAndClearArtemisState(): Promise<void> {
  // Clear local UI flags first so a dead/missing daemon cannot leave the
  // flags→models gate stuck after /new, /restart, or prompt clear.
  daemon.clearSwarmEvents()
  daemon.setLastModels([])
  daemon.setFlowCompleted(false)
  daemon.setSolveLocked(false)
  daemon.setSolveFlowBusy(false)
  daemon.setSuppressSolveGate(false)
  daemon.declinePendingFlagConfirm()

  if (daemon.swarmRunning[0]()) {
    try {
      await daemon.request("swarm_stop", {})
    } catch {
      /* ignore */
    }
  }
  try {
    await daemon.request("clear_session", {})
  } catch {
    /* ignore */
  }
}
