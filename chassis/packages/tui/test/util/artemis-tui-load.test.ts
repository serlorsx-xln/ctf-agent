import { beforeEach, describe, expect, mock, test } from "bun:test"
import { daemon } from "../../src/artemis/client"
import { tuiLoadChallenge } from "../../src/util/artemis-tui-load"

describe("tuiLoadChallenge", () => {
  beforeEach(() => {
    daemon.sessionState[1]({})
    daemon.flowCompleted[1](false)
  })

  test("error response does not apply stale session state", async () => {
    daemon.sessionState[1]({ challenge_name: "old", challenge_dir: "/tmp/old" })
    const loadChallenge = mock(async () => ({
      text: "ERROR: no such path",
      session_state: { challenge_name: "stale", challenge_dir: "/tmp/stale" },
    }))
    const orig = daemon.loadChallenge.bind(daemon)
    daemon.loadChallenge = loadChallenge as typeof daemon.loadChallenge
    try {
      const result = await tuiLoadChallenge("https://example.com/challenge")
      expect(result.status).toBe("error")
      expect(daemon.sessionState[0]()).toEqual({
        challenge_name: "old",
        challenge_dir: "/tmp/old",
      })
    } finally {
      daemon.loadChallenge = orig
    }
  })

  test("ok response returns session state", async () => {
    const loadChallenge = mock(async () => ({
      text: "Loaded challenge `pwn` at /tmp/pwn",
      session_state: { challenge_name: "pwn", challenge_dir: "/tmp/pwn" },
    }))
    const orig = daemon.loadChallenge.bind(daemon)
    daemon.loadChallenge = loadChallenge as typeof daemon.loadChallenge
    try {
      const result = await tuiLoadChallenge("https://example.com/challenge")
      expect(result.status).toBe("ok")
      if (result.status === "ok") {
        expect(result.session_state).toEqual({
          challenge_name: "pwn",
          challenge_dir: "/tmp/pwn",
        })
      }
    } finally {
      daemon.loadChallenge = orig
    }
  })
})
