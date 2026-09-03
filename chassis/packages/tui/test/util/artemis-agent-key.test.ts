import { describe, expect, test } from "bun:test"
import {
  agentKeyFromSpec,
  isReservedAgentKey,
  isReservedAgentTag,
  shortAgent,
  targetsMatch,
} from "../../src/util/artemis-agent-key"
import { formatSwarmElapsed } from "../../src/util/format"

describe("artemis-agent-key", () => {
  test("agentKeyFromSpec strips provider and effort", () => {
    expect(agentKeyFromSpec("cursor/auto")).toBe("auto")
    expect(agentKeyFromSpec("claude-sdk/aliyuncs/glm-5.2")).toBe("aliyuncs/glm-5.2")
    expect(agentKeyFromSpec("claude-sdk/bigmodel/glm-5.2/max")).toBe("bigmodel/glm-5.2")
    expect(agentKeyFromSpec("cursor/grok-4.5#2")).toBe("grok-4.5#2")
  })

  test("shortAgent matches agentKeyFromSpec for challenge/spec tags", () => {
    expect(shortAgent("chal/claude-sdk/aliyuncs/glm-5.2 think")).toBe("aliyuncs/glm-5.2")
    expect(shortAgent("chal/cursor/auto")).toBe("auto")
    expect(shortAgent("default")).toBe("default")
  })

  test("reserved tags", () => {
    expect(isReservedAgentTag("status")).toBe(true)
    expect(isReservedAgentTag("chal/artemis")).toBe(true)
    expect(isReservedAgentKey("boot")).toBe(true)
    expect(isReservedAgentKey("default")).toBe(false)
  })

  test("targetsMatch mirrors backend base↔#N rule", () => {
    expect(targetsMatch(null, "default#1")).toBe(true)
    expect(targetsMatch("opus", "opus#1")).toBe(true)
    expect(targetsMatch("opus#2", "opus")).toBe(true)
    expect(targetsMatch("opus", "composer#1")).toBe(false)
    expect(targetsMatch("default#1", "default#2")).toBe(false)
  })
})

describe("formatSwarmElapsed", () => {
  test("freezes when not running", () => {
    const started = 1_000_000
    expect(formatSwarmElapsed(started, started + 5_000, started + 60_000, false)).toBe("5s")
    expect(formatSwarmElapsed(started, null, started + 90_000, true)).toBe("1m 30s")
    expect(formatSwarmElapsed(null, null, Date.now(), true)).toBeNull()
  })
})
