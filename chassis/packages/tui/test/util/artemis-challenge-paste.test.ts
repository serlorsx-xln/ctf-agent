import { describe, expect, test } from "bun:test"
import {
  extractChallengePaths,
  isGreeting,
  loadArgsFromPrompt,
  looksLikeChallengePaste,
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
  })

  test("detects nc / flag format", () => {
    expect(looksLikeChallengePaste("Connect: nc 1.2.3.4 1337\nflag{...}")).toBe(true)
  })

  test("rejects short greetings only", () => {
    expect(isGreeting("hi")).toBe(true)
    expect(looksLikeChallengePaste("hi")).toBe(false)
    expect(looksLikeChallengePaste("hello")).toBe(false)
  })

  test("multi-line prose without CTF keywords still loads", () => {
    const text =
      "Line one of a normal chat paragraph that is quite long enough.\n" +
      "Line two continues the ordinary conversation without those keywords."
    expect(looksLikeChallengePaste(text)).toBe(true)
  })

  test("PwnKnight-style narrative loads", () => {
    const text =
      "A knight born of experiment, a fusion of code and magic.\n" +
      "PwnKnight walks the dungeon of darkness in a massive sandbox."
    expect(looksLikeChallengePaste(text)).toBe(true)
  })

  test("bare https and one-liners load", () => {
    expect(looksLikeChallengePaste("see https://docs.example.com/guide")).toBe(true)
    expect(looksLikeChallengePaste("Connect: https://chal.example.com:8443/")).toBe(true)
    expect(looksLikeChallengePaste("solve this")).toBe(true)
  })
})

describe("extractChallengePaths / loadArgsFromPrompt", () => {
  test("path-only prompt", () => {
    const text = "/Users/me/challenges/glass"
    expect(extractChallengePaths(text)).toEqual(["/Users/me/challenges/glass"])
    expect(looksLikeChallengePaste(text)).toBe(true)
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

  test("windows drive path with backslashes", () => {
    expect(extractChallengePaths(String.raw`C:\Users\me\challenges\glass please solve`)).toEqual([
      String.raw`C:\Users\me\challenges\glass`,
    ])
  })

  test("windows drive path with forward slashes", () => {
    expect(extractChallengePaths("C:/Users/me/challenges/glass please solve")).toEqual([
      "C:/Users/me/challenges/glass",
    ])
  })
})
