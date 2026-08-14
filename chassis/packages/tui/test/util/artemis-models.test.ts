import { afterEach, describe, expect, test } from "bun:test"
import { fromRaceSpec, isArtemisChatModel, toRaceSpec, typedConnectModelOptions } from "../../src/util/artemis-models"

afterEach(() => {
  delete process.env.ARTEMIS
})

describe("isArtemisChatModel", () => {
  test("allows Cursor/Claude/Codex/Gemini when ARTEMIS=1", () => {
    process.env.ARTEMIS = "1"
    expect(isArtemisChatModel({ providerID: "cursor", modelID: "composer-2.5" })).toBe(true)
    expect(isArtemisChatModel({ providerID: "anthropic", modelID: "claude-sonnet-4-5" })).toBe(true)
    expect(isArtemisChatModel({ providerID: "openai", modelID: "gpt-5.4" })).toBe(true)
    expect(isArtemisChatModel({ providerID: "google", modelID: "gemini-2.5-pro" })).toBe(true)
  })

  test("rejects Groq / Whisper / non-chat", () => {
    process.env.ARTEMIS = "1"
    expect(isArtemisChatModel({ providerID: "groq", modelID: "llama-3" })).toBe(false)
    expect(isArtemisChatModel({ providerID: "openai", modelID: "whisper-large-v3-turbo", name: "Whisper" })).toBe(
      false,
    )
    expect(
      isArtemisChatModel({
        providerID: "openai",
        modelID: "tts-1",
        modalities: { output: ["audio"] },
      }),
    ).toBe(false)
  })

  test("passthrough when not Artemis", () => {
    delete process.env.ARTEMIS
    expect(isArtemisChatModel({ providerID: "groq", modelID: "llama-3" })).toBe(true)
  })
})

describe("race spec mapping", () => {
  test("maps google ↔ gemini-sdk", () => {
    expect(toRaceSpec("google", "gemini-2.5-flash")).toBe("gemini-sdk/gemini-2.5-flash")
    expect(fromRaceSpec("gemini-sdk/gemini-2.5-pro")).toEqual({
      providerID: "google",
      modelID: "gemini-2.5-pro",
    })
    expect(fromRaceSpec("google/gemini-2.5-pro")).toEqual({
      providerID: "google",
      modelID: "gemini-2.5-pro",
    })
    expect(fromRaceSpec("gemini/gemini-2.5-flash")).toEqual({
      providerID: "google",
      modelID: "gemini-2.5-flash",
    })
  })

  test("accepts claude alias and strips effort suffix", () => {
    expect(fromRaceSpec("claude/claude-opus-4-6")).toEqual({
      providerID: "anthropic",
      modelID: "claude-opus-4-6",
    })
    expect(fromRaceSpec("claude-sdk/claude-opus-4-6/max")).toEqual({
      providerID: "anthropic",
      modelID: "claude-opus-4-6",
    })
    expect(fromRaceSpec("gemini-sdk/gemini-2.5-pro/max")).toEqual({
      providerID: "google",
      modelID: "gemini-2.5-pro",
    })
    expect(toRaceSpec("anthropic", "bigmodel/glm-5.2")).toBe("claude-sdk/bigmodel/glm-5.2")
    expect(fromRaceSpec("claude-sdk/bigmodel/glm-5.2")).toEqual({
      providerID: "anthropic",
      modelID: "bigmodel/glm-5.2",
    })
    expect(fromRaceSpec("claude-sdk/bigmodel/glm-5.2/max")).toEqual({
      providerID: "anthropic",
      modelID: "bigmodel/glm-5.2",
    })
  })
})

describe("typedConnectModelOptions", () => {
  test("adds a typed id for Claude when it is connected", () => {
    expect(typedConnectModelOptions("bigmodel/glm-5.2", ["anthropic"], [])).toEqual([
      { providerID: "anthropic", modelID: "bigmodel/glm-5.2" },
    ])
    expect(
      typedConnectModelOptions("bigmodel/glm-5.2", ["anthropic"], [
        { providerID: "anthropic", modelID: "bigmodel/glm-5.2" },
      ]),
    ).toEqual([])
    expect(typedConnectModelOptions("bigmodel/glm-5.2", ["cursor"], [])).toEqual([])
    expect(typedConnectModelOptions("has space", ["anthropic"], [])).toEqual([])
  })
})
