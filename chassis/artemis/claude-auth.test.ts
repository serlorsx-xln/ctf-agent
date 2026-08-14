import { describe, expect, test } from "bun:test"
import {
  anthropicSdkBaseUrl,
  normalizeAnthropicBaseUrl,
  normalizeModelsListUrl,
  resolveModelsListUrl,
  siblingOpenaiModelsUrl,
} from "./claude-auth"

describe("normalizeAnthropicBaseUrl", () => {
  test("keeps the URL the operator already uses with Claude CLI", () => {
    expect(normalizeAnthropicBaseUrl("https://host/anthropic")).toBe("https://host/anthropic")
    expect(normalizeAnthropicBaseUrl("https://host/anthropic/")).toBe("https://host/anthropic")
    expect(normalizeAnthropicBaseUrl("https://host/anthropic/v1")).toBe("https://host/anthropic")
    expect(normalizeAnthropicBaseUrl("https://api.anthropic.com")).toBe("https://api.anthropic.com")
  })

  test("SDK base URL adds /v1 so chat hits /v1/messages like Claude CLI", () => {
    expect(anthropicSdkBaseUrl("https://host/anthropic")).toBe("https://host/anthropic/v1")
    expect(anthropicSdkBaseUrl("https://host/anthropic/v1")).toBe("https://host/anthropic/v1")
    expect(anthropicSdkBaseUrl("https://api.anthropic.com")).toBe("https://api.anthropic.com/v1")
  })
})

describe("siblingOpenaiModelsUrl", () => {
  test("lists /v1/models on the same host without assuming a vendor", () => {
    expect(siblingOpenaiModelsUrl("https://host/anthropic")).toBe("https://host/v1/models")
    expect(siblingOpenaiModelsUrl("https://api.anthropic.com")).toBe("")
  })
})

describe("resolveModelsListUrl", () => {
  test("uses the pasted models URL when the operator sets one", () => {
    expect(normalizeModelsListUrl("https://other/v1/models")).toBe("https://other/v1/models")
    expect(normalizeModelsListUrl("https://other/v1")).toBe("https://other/v1/models")
    expect(resolveModelsListUrl("https://other/v1/models", "https://host/anthropic")).toBe("https://other/v1/models")
  })

  test("falls back to the chat host catalog when models URL is skipped", () => {
    expect(resolveModelsListUrl("", "https://host/anthropic")).toBe("https://host/v1/models")
    expect(resolveModelsListUrl("", "https://api.anthropic.com")).toBe("")
  })
})
