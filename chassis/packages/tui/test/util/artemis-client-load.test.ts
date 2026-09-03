import { beforeEach, describe, expect, mock, test } from "bun:test"
import { daemon } from "../../src/artemis/client"

describe("daemon.loadChallenge session apply", () => {
  beforeEach(() => {
    daemon.sessionState[1]({})
  })

  test("ERROR text does not apply session_state", async () => {
    const request = mock(async () => ({
      text: "ERROR: no such path",
      session_state: { challenge_name: "stale", challenge_dir: "/tmp/stale" },
    }))
    const origRequest = daemon.request.bind(daemon)
    const origEnsure = daemon.ensureConnected.bind(daemon)
    daemon.request = request as typeof daemon.request
    daemon.ensureConnected = async () => {}
    try {
      const res = await daemon.loadChallenge({ path: "/nope" })
      expect(res.text).toMatch(/^ERROR/i)
      expect(daemon.sessionState[0]()).toEqual({})
    } finally {
      daemon.request = origRequest
      daemon.ensureConnected = origEnsure
    }
  })

  test("successful load applies session_state", async () => {
    const request = mock(async () => ({
      text: "Loaded challenge `pwn`",
      session_state: { challenge_name: "pwn", challenge_dir: "/tmp/pwn" },
    }))
    const origRequest = daemon.request.bind(daemon)
    const origEnsure = daemon.ensureConnected.bind(daemon)
    daemon.request = request as typeof daemon.request
    daemon.ensureConnected = async () => {}
    try {
      await daemon.loadChallenge({ path: "/tmp/pwn" })
      expect(daemon.sessionState[0]()).toEqual({
        challenge_name: "pwn",
        challenge_dir: "/tmp/pwn",
      })
    } finally {
      daemon.request = origRequest
      daemon.ensureConnected = origEnsure
    }
  })
})
