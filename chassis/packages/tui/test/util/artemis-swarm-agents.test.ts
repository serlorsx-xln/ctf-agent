import { describe, expect, test } from "bun:test"
import {
  agentKeyFromSpec,
  agentPreviews,
  globalSwarmEvents,
  listSwarmAgents,
  partitionEventsByAgent,
  rosterFromDaemonPush,
  rosterFromSpecs,
  winnerAgents,
} from "../../src/util/artemis-swarm-agents"
import type { ArtemisEvent } from "../../src/util/artemis-live-log"

describe("artemis-swarm-agents", () => {
  test("slashy Claude ids match live-log agent keys", () => {
    const spec = "claude-sdk/aliyuncs/glm-5.2"
    const key = agentKeyFromSpec(spec)
    expect(key).toBe("aliyuncs/glm-5.2")
    const events: ArtemisEvent[] = [{ kind: "think", agent: key, text: "hi" }]
    expect(rosterFromSpecs([spec])).toEqual([key])
    expect(partitionEventsByAgent(events, key)).toHaveLength(1)
    expect(agentPreviews(events, [key], true)[0]).toMatchObject({
      eventCount: 1,
      active: true,
    })
  })

  test("rosterFromSpecs matches assign_runner_ids labels", () => {
    expect(rosterFromSpecs(["cursor/auto", "claude-sdk/claude-opus-4-6"])).toEqual([
      "auto",
      "claude-opus-4-6",
    ])
    expect(rosterFromSpecs(["cursor/grok-4.5", "cursor/grok-4.5"])).toEqual([
      "grok-4.5#1",
      "grok-4.5#2",
    ])
  })

  test("slashy Claude picks stay one box each", () => {
    const specs = [
      "claude-sdk/PSU-araya/psu-gemma",
      "claude-sdk/aliyuncs/MiniMax-M2.1",
      "claude-sdk/aliyuncs/MiniMax-M2.5",
      "claude-sdk/aliyuncs/glm-4.7",
    ]
    expect(rosterFromSpecs(specs)).toEqual([
      "PSU-araya/psu-gemma",
      "aliyuncs/MiniMax-M2.1",
      "aliyuncs/MiniMax-M2.5",
      "aliyuncs/glm-4.7",
    ])
    expect(rosterFromDaemonPush(["PSU-araya", "aliyuncs"], specs)).toEqual(rosterFromSpecs(specs))
    expect(listSwarmAgents([], specs, ["PSU-araya", "aliyuncs"])).toEqual(rosterFromSpecs(specs))
  })

  test("listSwarmAgents roster is exclusive — no leftover log agents", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "auto", text: "hi" },
      { kind: "bash", agent: "extra", command: "ls" },
      { kind: "think", agent: "glm-5.2", text: "stale" },
    ]
    expect(listSwarmAgents(events, ["cursor/auto"], ["auto", "opus"])).toEqual(["auto", "opus"])
  })

  test("listSwarmAgents falls back to specs then log when roster empty", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "auto", text: "hi" },
      { kind: "bash", agent: "extra", command: "ls" },
    ]
    expect(listSwarmAgents(events, ["cursor/auto"], [])).toEqual(["auto"])
    expect(listSwarmAgents(events, [], [])).toEqual(["auto", "extra"])
  })

  test("partition and global events", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "a", text: "t1" },
      { kind: "think", agent: "b", text: "t2" },
      { kind: "outcome", level: "error", text: "Cursor usage limit reached" },
      { kind: "flag_confirm", id: "1", flag: "flag{x}" },
    ]
    expect(partitionEventsByAgent(events, "a")).toHaveLength(1)
    // flag_confirm is owned by FlagConfirmBar — not duplicated in the feed.
    expect(globalSwarmEvents(events).map((e) => e.kind)).toEqual(["outcome"])
  })

  test("agentPreviews", () => {
    const events: ArtemisEvent[] = [{ kind: "bash", agent: "auto", command: "id" }]
    const previews = agentPreviews(events, ["auto", "opus"], true)
    expect(previews[0]).toMatchObject({ agent: "auto", active: true, eventCount: 1, failed: false })
    expect(previews[0]?.lastLine).toContain("$ id")
    expect(previews[1]).toMatchObject({
      agent: "opus",
      active: false,
      eventCount: 0,
      lastLine: "starting…",
      failed: false,
    })
  })

  test("agentPreviews: lineCounts keep eventCount monotonic when coalesce shrinks the list", () => {
    const events: ArtemisEvent[] = [{ kind: "think", agent: "auto", text: "hi" }]
    const previews = agentPreviews(events, ["auto"], true, { auto: 12 })
    expect(previews[0]).toMatchObject({ agent: "auto", eventCount: 12 })
  })

  test("agentPreviews: not-started siblings stay starting while swarm runs (even if global quota exists)", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "default", text: "hi" },
      {
        kind: "status",
        agent: "default",
        text: "Cursor usage limit reached — switch model",
      },
      { kind: "outcome", level: "error", text: "Cursor usage limit reached — switch model" },
    ]
    const whileRunning = agentPreviews(events, ["default", "composer-2.5", "grok-4.5"], true)
    expect(whileRunning.find((p) => p.agent === "default")).toMatchObject({
      failed: true,
      // Must not keep the last think line — that looked like the agent was still live.
      lastLine: "Cursor usage limit reached — switch model",
    })
    expect(whileRunning.find((p) => p.agent === "composer-2.5")).toMatchObject({
      failed: false,
      eventCount: 0,
      lastLine: "starting…",
    })
    expect(whileRunning.find((p) => p.agent === "grok-4.5")).toMatchObject({
      failed: false,
      eventCount: 0,
      lastLine: "starting…",
    })

    const afterExit = agentPreviews(events, ["default", "composer-2.5", "grok-4.5"], false)
    expect(afterExit.every((p) => p.failed)).toBe(true)
    expect(afterExit.every((p) => /usage limit/i.test(p.lastLine))).toBe(true)
  })

  test("agentPreviews: quota death replaces the last bash line on the box", () => {
    const events: ArtemisEvent[] = [
      { kind: "bash", agent: "composer-2.5", command: "python3 solve.py" },
      {
        kind: "outcome",
        level: "error",
        text: "Cursor usage limit reached — switch model or wait for reset",
        agent: "composer-2.5",
      },
    ]
    const [p] = agentPreviews(events, ["composer-2.5"], true)
    expect(p).toMatchObject({
      failed: true,
      active: false,
      lastLine: "Cursor usage limit reached — switch model or wait for reset",
    })
    expect(p?.lastLine).not.toContain("solve.py")
  })

  test("listSwarmAgents ignores phantom status agent from quota tags", () => {
    const events: ArtemisEvent[] = [
      { kind: "outcome", level: "error", text: "Cursor usage limit reached", agent: "status" },
      { kind: "status", text: "noise", agent: "status" },
      { kind: "think", agent: "claude-fable-5", text: "hi" },
    ]
    expect(
      listSwarmAgents(events, ["cursor/default", "cursor/claude-fable-5", "cursor/glm-4.2"], [
        "default",
        "claude-fable-5",
        "glm-4.2",
      ]),
    ).toEqual(["default", "claude-fable-5", "glm-4.2"])
  })

  test("winnerAgents reads the credited agents off the recap", () => {
    expect(winnerAgents([{ kind: "summary", text: "Solved by grok-4.5" }])).toEqual(["grok-4.5"])
    expect(winnerAgents([{ kind: "summary", text: "Solved by grok-4.5, composer-2.5" }])).toEqual([
      "grok-4.5",
      "composer-2.5",
    ])
    expect(winnerAgents([{ kind: "summary", text: "Solved by the swarm" }])).toEqual([])
    expect(winnerAgents([{ kind: "status", text: "Solved by grok-4.5" }])).toEqual([])
  })

  test("winnerAgents ignores interim Writing recap placeholder", () => {
    expect(
      winnerAgents([
        { kind: "summary", text: "Writing recap from the winning solver…" },
        {
          kind: "outcome",
          level: "success",
          text: 'CORRECT — accepted "flag{x}" via opus. Challenge complete for this run.',
        },
      ]),
    ).toEqual(["opus"])
  })

  test("winnerAgents falls back to via agent on CORRECT when Solved by is missing", () => {
    expect(
      winnerAgents([
        {
          kind: "outcome",
          level: "success",
          text: 'CORRECT — accepted "flag{x}" via opus#2. Challenge complete for this run.',
        },
      ]),
    ).toEqual(["opus#2"])
    expect(
      winnerAgents([
        { kind: "outcome", level: "success", text: 'ACCEPTED "a" via default (1/2).' },
        {
          kind: "outcome",
          level: "success",
          text: 'CORRECT — accepted all 2 flags: a | b (last via composer-2.5). Challenge complete.',
        },
      ]),
    ).toEqual(["composer-2.5"])
    // Backend emits runner_id (provider/spec); normalize to roster display key.
    expect(
      winnerAgents([
        {
          kind: "outcome",
          level: "success",
          text: 'CORRECT — accepted "flag{x}" via cursor/composer-2.5. Challenge complete.',
        },
      ]),
    ).toEqual(["composer-2.5"])
    // Mid-run ACCEPTED must not credit a winner before CORRECT.
    expect(
      winnerAgents([
        {
          kind: "outcome",
          level: "success",
          text: 'ACCEPTED "a" via default (1/2).',
        },
      ]),
    ).toEqual([])
  })

  test("agentPreviews marks the agent that earned the flag", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "grok-4.5", text: "looking" },
      { kind: "outcome", level: "error", text: "Cursor usage limit reached", agent: "composer-2.5" },
      { kind: "summary", text: "Solved by grok-4.5" },
    ]
    const [winner, loser] = agentPreviews(events, ["grok-4.5", "composer-2.5"], false)
    expect(winner).toMatchObject({ won: true, failed: false, lastLine: "solved · flag accepted" })
    expect(loser).toMatchObject({
      won: false,
      failed: true,
      lastLine: "Cursor usage limit reached",
    })
  })

  test("agentPreviews winner shows hold/Q&A while process lingers after CORRECT", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "grok-4.5", text: "looking" },
      { kind: "summary", text: "Solved by grok-4.5" },
      { kind: "status", text: "Hold — ask follow-ups" },
    ]
    const [winner] = agentPreviews(events, ["grok-4.5"], true, {}, { solving: false })
    expect(winner).toMatchObject({ won: true, active: false, lastLine: "solved · hold/Q&A" })
  })

  test("agentPreviews stops live/active when solving=false during Hold linger", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "grok-4.5", text: "done" },
      { kind: "think", agent: "composer-2.5", text: "also" },
      { kind: "summary", text: "Solved by grok-4.5" },
    ]
    const whileSolving = agentPreviews(events, ["grok-4.5", "composer-2.5"], true, {}, { solving: true })
    expect(whileSolving[0]?.active).toBe(true)
    expect(whileSolving[1]?.active).toBe(true)
    const onHold = agentPreviews(events, ["grok-4.5", "composer-2.5"], true, {}, { solving: false })
    expect(onHold[0]).toMatchObject({ won: true, active: false })
    // Sibling keeps last think text but must not be active/live.
    expect(onHold[1]).toMatchObject({ won: false, active: false, lastLine: "also" })
    const quiet = agentPreviews(
      [{ kind: "summary", text: "Solved by grok-4.5" }],
      ["composer-2.5"],
      true,
      {},
      { solving: false },
    )
    expect(quiet[0]).toMatchObject({ active: false, lastLine: "idle · hold/writeup" })
  })

  test("globalSwarmEvents keeps mixed-swarm quota WARN on the main page", () => {
    const events: ArtemisEvent[] = [
      {
        kind: "outcome",
        level: "warn",
        text: "WARN — composer-2.5 hit a usage limit (siblings continue)",
      },
      {
        kind: "outcome",
        level: "error",
        text: "Cursor usage limit reached",
        agent: "composer-2.5",
      },
    ]
    expect(globalSwarmEvents(events).map((e) => (e.kind === "outcome" ? e.level : undefined))).toEqual([
      "warn",
    ])
  })

  test("boot lines name their container without counting as agent work", () => {
    const events: ArtemisEvent[] = [
      { kind: "boot", text: "grok-4.5 Starting Docker sandbox", agent: "grok-4.5" },
    ]
    expect(partitionEventsByAgent(events, "grok-4.5")).toEqual([])
    expect(agentPreviews(events, ["grok-4.5"], true)[0]).toMatchObject({
      eventCount: 0,
      lastLine: "starting…",
    })
    expect(globalSwarmEvents(events)).toHaveLength(1)
  })

  test("globalSwarmEvents keeps the run recap on the main page", () => {
    const events: ArtemisEvent[] = [{ kind: "summary", text: "Solved by grok-4.5" }]
    expect(globalSwarmEvents(events)).toEqual(events)
  })

  test("globalSwarmEvents keeps operator Send now / Queue crumbs", () => {
    const events: ArtemisEvent[] = [
      { kind: "operator", text: "hi", delivery: "steer" },
      { kind: "operator", text: "later", delivery: "queue" },
    ]
    expect(globalSwarmEvents(events)).toEqual(events)
  })

  test("listSwarmAgents with roster ignores stale default/glm from prior run", () => {
    const events: ArtemisEvent[] = [
      { kind: "think", agent: "default", text: "old" },
      { kind: "think", agent: "glm-5.2", text: "old" },
      { kind: "think", agent: "claude-opus-4-8", text: "old" },
      { kind: "think", agent: "claude-fable-5", text: "new" },
    ]
    expect(
      listSwarmAgents(events, [], ["claude-fable-5", "claude-sonnet-5", "claude-opus-4-8"]),
    ).toEqual(["claude-fable-5", "claude-sonnet-5", "claude-opus-4-8"])
  })
})
