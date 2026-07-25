import { beforeEach, describe, expect, test } from "bun:test"
import { daemon } from "../../src/artemis/client"
import {
  confirmRestartOpts,
  hasActiveSolveWork,
  hasDaemonArtemisResidue,
  solveGateOpen,
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
})
