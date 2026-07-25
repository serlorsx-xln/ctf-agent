import { describe, expect, test } from "bun:test"
import {
  extractChallengePaths,
  loadArgsFromPrompt,
  looksLikeChallengePaste,
  shouldLoadChallengeFromPrompt,
} from "../../src/util/artemis-challenge-paste"

describe("looksLikeChallengePaste", () => {
  test("detects ssh + password paste", () => {
    const text = [
      "This is a simple CRC calculator for kernel module programming exercise.",
      "I bet there are no bugs.",
      "but you can check it if you want.",
      "",
      "ssh kcrc@pwnable.kr -p2222 (pw: guest)",
    ].join("\n")
    expect(looksLikeChallengePaste(text)).toBe(true)
    expect(shouldLoadChallengeFromPrompt(text)).toBe(true)
  })

  test("detects nc / flag format", () => {
    expect(looksLikeChallengePaste("Connect: nc 1.2.3.4 1337\nflag{...}")).toBe(true)
  })

  test("rejects short greetings", () => {
    expect(looksLikeChallengePaste("hi")).toBe(false)
    expect(shouldLoadChallengeFromPrompt("hello")).toBe(false)
  })

  test("multi-line long prose without CTF anchors is rejected", () => {
    const text =
      "Line one of a normal chat paragraph that is quite long enough.\n" +
      "Line two continues the ordinary conversation without those keywords."
    expect(looksLikeChallengePaste(text)).toBe(false)
  })

  test("multi-line CTF prose is accepted", () => {
    const text =
      "This challenge hides a secret flag in the binary.\n" +
      "You should reverse it carefully and extract the answer."
    expect(looksLikeChallengePaste(text)).toBe(true)
  })

  test("bare https URL is not enough", () => {
    expect(looksLikeChallengePaste("see https://docs.example.com/guide")).toBe(false)
  })

  test("https with explicit port is enough", () => {
    expect(looksLikeChallengePaste("Connect: https://chal.example.com:8443/")).toBe(true)
  })
})

describe("extractChallengePaths / loadArgsFromPrompt", () => {
  test("path-only prompt", () => {
    const text = "/Users/me/challenges/glass"
    expect(extractChallengePaths(text)).toEqual(["/Users/me/challenges/glass"])
    expect(shouldLoadChallengeFromPrompt(text)).toBe(true)
    expect(loadArgsFromPrompt(text)).toEqual({
      mode: "artemis",
      path: "/Users/me/challenges/glass",
    })
  })

  test("path + paste", () => {
    const text = "/Users/me/challenges/foo\nnc 1.2.3.4 9999"
    const args = loadArgsFromPrompt(text)
    expect(args.path).toBe("/Users/me/challenges/foo")
    expect(args.prompt).toContain("nc 1.2.3.4 9999")
  })
})
