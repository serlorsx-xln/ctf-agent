import { describe, expect, test } from "bun:test"
import {
  classifyArtemisPromptSubmit,
  looksLikeChallengeReload,
  parseOperatorDelivery,
} from "../../src/util/artemis-prompt-intent"

describe("classifyArtemisPromptSubmit", () => {
  test("challenge paste wins when idle", () => {
    expect(
      classifyArtemisPromptSubmit({
        raw: "flag is at nc 1.2.3.4 1337",
        looksLikeChallenge: true,
        swarmRunning: false,
        solveLocked: false,
        hasChallenge: false,
        flowCompleted: false,
        hasPriorState: false,
      }).kind,
    ).toBe("challenge_paste")
  })

  test("mid-solve short chat steers even if looksLikeChallenge", () => {
    const intent = classifyArtemisPromptSubmit({
      raw: "เป็นไงบ้าง",
      looksLikeChallenge: true, // first-message heuristic is broad
      swarmRunning: true,
      solveLocked: true,
      hasChallenge: true,
      flowCompleted: false,
      hasPriorState: true,
    })
    expect(intent).toEqual({ kind: "steer_swarm", text: "เป็นไงบ้าง" })
  })

  test("mid-solve text steers swarm", () => {
    const intent = classifyArtemisPromptSubmit({
      raw: "check the JWT secret in .env",
      looksLikeChallenge: false,
      swarmRunning: true,
      solveLocked: true,
      hasChallenge: true,
      flowCompleted: false,
      hasPriorState: true,
    })
    expect(intent).toEqual({ kind: "steer_swarm", text: "check the JWT secret in .env" })
  })

  test("mid-solve path paste reloads challenge", () => {
    expect(looksLikeChallengeReload("/Users/me/ctf/chal")).toBe(true)
    expect(
      classifyArtemisPromptSubmit({
        raw: "/Users/me/ctf/chal",
        looksLikeChallenge: true,
        swarmRunning: true,
        solveLocked: false,
        hasChallenge: true,
        flowCompleted: false,
        hasPriorState: true,
      }).kind,
    ).toBe("challenge_paste")
  })

  test("mid-solve /queue prefixes queue delivery", () => {
    expect(parseOperatorDelivery("/queue try /admin")).toMatchObject({
      delivery: "queue",
      text: "try /admin",
      bareQueue: false,
    })
    const intent = classifyArtemisPromptSubmit({
      raw: "/queue try /admin after this tool",
      looksLikeChallenge: false,
      swarmRunning: true,
      solveLocked: false,
      hasChallenge: true,
      flowCompleted: false,
      hasPriorState: true,
    })
    expect(intent).toEqual({
      kind: "queue_swarm",
      text: "try /admin after this tool",
    })
  })

  test("bare /queue does not stop the swarm", () => {
    expect(
      classifyArtemisPromptSubmit({
        raw: "/queue",
        looksLikeChallenge: false,
        swarmRunning: true,
        solveLocked: false,
        hasChallenge: true,
        flowCompleted: false,
        hasPriorState: true,
      }),
    ).toEqual({ kind: "queue_swarm", text: "" })
  })

  test("empty Enter mid-solve stops without clear/restart", () => {
    expect(
      classifyArtemisPromptSubmit({
        raw: "   ",
        looksLikeChallenge: false,
        swarmRunning: true,
        solveLocked: false,
        hasChallenge: true,
        flowCompleted: false,
        hasPriorState: true,
      }).kind,
    ).toBe("stop_swarm")
  })

  test("empty Enter during post-CORRECT hold stops without clear/restart", () => {
    expect(
      classifyArtemisPromptSubmit({
        raw: "",
        looksLikeChallenge: false,
        swarmRunning: true,
        solveLocked: false,
        hasChallenge: true,
        flowCompleted: true,
        hasPriorState: true,
      }).kind,
    ).toBe("stop_swarm")
  })

  test("post-CORRECT hold still routes to solvers while swarm alive", () => {
    const intent = classifyArtemisPromptSubmit({
      raw: "why did XOR work?",
      looksLikeChallenge: false,
      swarmRunning: true,
      solveLocked: false,
      hasChallenge: true,
      flowCompleted: true,
      hasPriorState: true,
    })
    expect(intent).toEqual({ kind: "steer_swarm", text: "why did XOR work?" })
  })

  test("after complete and swarm exited allows OpenCode chat", () => {
    expect(
      classifyArtemisPromptSubmit({
        raw: "how did you find it?",
        looksLikeChallenge: false,
        swarmRunning: false,
        solveLocked: false,
        hasChallenge: true,
        flowCompleted: true,
        hasPriorState: true,
      }).kind,
    ).toBe("chat_ok")
  })

  test("loaded but not started waits", () => {
    expect(
      classifyArtemisPromptSubmit({
        raw: "hello",
        looksLikeChallenge: false,
        swarmRunning: false,
        solveLocked: false,
        hasChallenge: true,
        flowCompleted: false,
        hasPriorState: true,
      }).kind,
    ).toBe("wait_for_start")
  })
})
