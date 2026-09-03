import { beforeEach, describe, expect, mock, test } from "bun:test"
import { daemon } from "../../src/artemis/client"

function resetSwarmUi() {
  daemon.swarmRunning[1](false)
  daemon.solveLocked[1](false)
  daemon.solveFlowBusy[1](false)
  daemon.flowCompleted[1](false)
  daemon.suppressSolveGate[1](false)
  daemon.swarmEvents[1]([])
  daemon.lastModels[1](["cursor/a", "claude/b"])
  daemon.swarmStartedAt[1](null)
  daemon.swarmEndedAt[1](null)
}

function dispatch(msg: Record<string, unknown>) {
  ;(daemon as unknown as { dispatch: (m: Record<string, unknown>) => void }).dispatch({
    v: 1,
    id: null,
    ...msg,
  })
}

describe("daemon reconnect pushes", () => {
  beforeEach(resetSwarmUi)

  test("swarm_adopted sets started_at when local clock unknown", () => {
    dispatch({ type: "swarm_adopted", started_at: 1_700_000_000_000 })
    expect(daemon.swarmRunning[0]()).toBe(true)
    expect(daemon.swarmStartedAt[0]()).toBe(1_700_000_000_000)
  })

  test("swarm_adopted does not overwrite local started_at", () => {
    daemon.swarmStartedAt[1](1_700_000_000_001)
    dispatch({ type: "swarm_adopted", started_at: 1_700_000_000_000 })
    expect(daemon.swarmStartedAt[0]()).toBe(1_700_000_000_001)
  })

  test("replay_done running+terminal freezes elapsed and keeps swarmRunning", () => {
    daemon.swarmEvents[1]([
      {
        kind: "outcome",
        level: "success",
        text: "CORRECT — accepted flag. Challenge complete for this run.",
      },
    ])
    const before = Date.now()
    dispatch({ type: "replay_done", running: true, started_at: before - 60_000 })
    expect(daemon.swarmRunning[0]()).toBe(true)
    expect(daemon.solveLocked[0]()).toBe(false)
    expect(daemon.suppressSolveGate[0]()).toBe(true)
    expect(daemon.swarmStartedAt[0]()).toBeGreaterThan(0)
    expect(daemon.swarmEndedAt[0]()).toBeGreaterThanOrEqual(before)
  })

  test("replay_done idle+terminal preserves feed and suppresses gate", () => {
    daemon.swarmStartedAt[1](Date.now() - 5000)
    daemon.swarmEvents[1]([
      {
        kind: "outcome",
        level: "success",
        text: "CORRECT — accepted flag. Challenge complete for this run.",
      },
    ])
    dispatch({ type: "replay_done", running: false })
    expect(daemon.swarmRunning[0]()).toBe(false)
    expect(daemon.swarmEvents[0]()).toHaveLength(1)
    expect(daemon.suppressSolveGate[0]()).toBe(true)
    expect(daemon.swarmEndedAt[0]()).not.toBeNull()
  })

  test("replay_done idle without terminal clears stale events", () => {
    daemon.swarmEvents[1]([{ kind: "think", agent: "a", text: "old" }])
    dispatch({ type: "replay_done", running: false })
    expect(daemon.swarmEvents[0]()).toHaveLength(0)
    expect(daemon.suppressSolveGate[0]()).toBe(false)
  })

  test("replay_done ignored while solve flow gate is busy", () => {
    daemon.solveFlowBusy[1](true)
    daemon.swarmEvents[1]([{ kind: "think", agent: "a", text: "keep" }])
    dispatch({ type: "replay_done", running: false })
    expect(daemon.swarmEvents[0]()).toHaveLength(1)
  })
})

describe("daemon.shutdown", () => {
  beforeEach(() => {
    resetSwarmUi()
    ;(daemon as unknown as { closed: boolean }).closed = false
  })

  test("calls swarm_stop when socket is connected", async () => {
    const calls: string[] = []
    const origRequest = daemon.request.bind(daemon)
    daemon.request = (async (type: string) => {
      calls.push(type)
      return {}
    }) as typeof daemon.request
    ;(daemon as unknown as { sock: { destroyed: boolean } }).sock = { destroyed: false }
    try {
      await daemon.shutdown()
      expect(calls).toContain("swarm_stop")
      expect(daemon.swarmRunning[0]()).toBe(false)
    } finally {
      daemon.request = origRequest
      ;(daemon as unknown as { sock: null }).sock = null
    }
  })
})
