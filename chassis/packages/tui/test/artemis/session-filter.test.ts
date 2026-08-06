import { describe, expect, test } from "bun:test"
import { pushBelongsToSession } from "../../src/artemis/client"

describe("pushBelongsToSession", () => {
  test("accepts unscoped pushes", () => {
    expect(pushBelongsToSession(null, "win-a")).toBe(true)
    expect(pushBelongsToSession(undefined, "win-a")).toBe(true)
    expect(pushBelongsToSession("", "win-a")).toBe(true)
  })

  test("accepts all pushes when TUI has no session yet", () => {
    expect(pushBelongsToSession("win-a", null)).toBe(true)
  })

  test("filters other sessions", () => {
    expect(pushBelongsToSession("win-a", "win-a")).toBe(true)
    expect(pushBelongsToSession("win-b", "win-a")).toBe(false)
  })
})
