import { beforeEach, describe, expect, test } from "bun:test"
import { daemon } from "../../src/artemis/client"
import {
  confirmRestartOpts,
  hasActiveSolveWork,
  hasDaemonArtemisResidue,
  solveGateOpen,
  stopAndClearArtemisState,
  swarmHasVisibleSolveUi,
  warnIfSolveGateOpen,
} from "../../src/util/artemis-solve-state"

function reset() {
  daemon.swarmRunning[1](false)
  daemon.solveLocked[1](false)
  daemon.solveFlowBusy[1](false)
  daemon.flowCompleted[1](false)
  daemon.suppressSolveGate[1](false)
  daemon.swarmEvents[1]([])
  daemon.swarmRoster[1]([])
  daemon.lastModels[1]([])
  daemon.sessionState[1]({})
}

describe("artemis-solve-state", () => {
  beforeEach(reset)

  test("idle daemon has no work and no residue", () => {
    expect(hasActiveSolveWork()).toBe(false)
    expect(hasDaemonArtemisResidue()).toBe(false)
    expect(confirmRestartOpts()).toEqual({ active: false, completed: false })
  })

  test("running swarm counts as active work", () => {
    daemon.swarmRunning[1](true)
    expect(hasActiveSolveWork()).toBe(true)
    expect(confirmRestartOpts()).toEqual({ active: true, completed: false })
  })

  test("solve lock during the flags gate is not active work", () => {
    daemon.solveLocked[1](true)
    daemon.solveFlowBusy[1](true)
    expect(hasActiveSolveWork()).toBe(false)
    expect(solveGateOpen()).toBe(true)
  })

  test("finished solve asks to start a new challenge", () => {
    daemon.flowCompleted[1](true)
    expect(hasDaemonArtemisResidue()).toBe(true)
    expect(confirmRestartOpts()).toEqual({ active: false, completed: true })
  })

  test("a loaded challenge alone is residue", () => {
    daemon.sessionState[1]({ challenge_name: "pwnknight" })
    expect(hasActiveSolveWork()).toBe(false)
    expect(hasDaemonArtemisResidue()).toBe(true)
    // /new, /restart and prompt submit must all see this same residue.
    expect(confirmRestartOpts()).toEqual({ active: false, completed: false })
  })

  test("warnIfSolveGateOpen blocks only while the gate is busy", () => {
    const shown: string[] = []
    expect(warnIfSolveGateOpen((opts) => shown.push(opts.message))).toBe(false)
    daemon.solveFlowBusy[1](true)
    expect(warnIfSolveGateOpen((opts) => shown.push(opts.message))).toBe(true)
    expect(shown.length).toBe(1)
  })

  test("idle load does not show an empty Solve card", () => {
    expect(
      swarmHasVisibleSolveUi({ running: false, eventCount: 0, startedAt: null }),
    ).toBe(false)
    expect(
      swarmHasVisibleSolveUi({ running: true, eventCount: 0, startedAt: Date.now() }),
    ).toBe(true)
  })

  test("stopAndClear drops a leaked solveFlowBusy", async () => {
    daemon.solveFlowBusy[1](true)
    daemon.suppressSolveGate[1](true)
    daemon.solveLocked[1](true)
    await stopAndClearArtemisState()
    expect(daemon.solveFlowBusy[0]()).toBe(false)
    expect(daemon.suppressSolveGate[0]()).toBe(false)
    expect(daemon.solveLocked[0]()).toBe(false)
    expect(solveGateOpen()).toBe(false)
  })
})
