import { describe, expect, test } from "bun:test"
import { normalizeCustomProviderID, providerOptions } from "../../../../src/component/dialog-provider"

describe("providerOptions", () => {
  test("includes a synthetic Other option for custom providers", () => {
    delete process.env.ARTEMIS
    expect(providerOptions([{ id: "openai", name: "OpenAI" }]).at(-1)).toMatchObject({
      title: "Other",
      description: "Custom provider",
      category: "Providers",
    })
  })

  test("does not use Other as the generic provider category", () => {
    delete process.env.ARTEMIS
    expect(providerOptions([{ id: "mistral", name: "Mistral" }])[0]?.category).toBe("Providers")
  })

  test("keeps Artemis chat providers first and sorts the rest alphabetically", () => {
    delete process.env.ARTEMIS
    expect(
      providerOptions([
        { id: "openai", name: "OpenAI" },
        { id: "custom-z", name: "Zebra Provider" },
        { id: "anthropic", name: "Anthropic" },
        { id: "cursor", name: "Cursor" },
        { id: "mistral", name: "Mistral" },
        { id: "aws", name: "AWS Bedrock" },
      ]).map((option) => option.value),
    ).toEqual(["cursor", "anthropic", "openai", "aws", "mistral", "custom-z", "__opencode_custom_provider__"])
  })

  test("under ARTEMIS only Cursor + Claude + Codex + Gemini remain", () => {
    process.env.ARTEMIS = "1"
    expect(
      providerOptions([
        { id: "openai", name: "OpenAI" },
        { id: "groq", name: "Groq" },
        { id: "anthropic", name: "Anthropic" },
        { id: "cursor", name: "Cursor" },
        { id: "google", name: "Google" },
        { id: "mistral", name: "Mistral" },
      ]).map((option) => option.value),
    ).toEqual(["cursor", "anthropic", "openai", "google"])
    delete process.env.ARTEMIS
  })

  test("does not collide with a configured provider named other", () => {
    delete process.env.ARTEMIS
    const values = providerOptions([{ id: "other", name: "Other Provider" }]).map((option) => option.value)
    expect(new Set(values).size).toBe(values.length)
  })

  test("normalizes and validates custom provider ids", () => {
    expect(normalizeCustomProviderID("  custom-provider  ")).toBe("custom-provider")
    expect(normalizeCustomProviderID("custom_provider")).toBe("custom_provider")
    expect(normalizeCustomProviderID("@ai-sdk/custom-provider")).toBe("custom-provider")
    expect(normalizeCustomProviderID("-custom-provider")).toBeUndefined()
    expect(normalizeCustomProviderID("Custom Provider")).toBeUndefined()
  })
})
