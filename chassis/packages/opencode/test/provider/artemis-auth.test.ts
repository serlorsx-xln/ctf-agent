import { describe, expect, test } from "bun:test"
import {
  artemisAuthenticatedProviders,
  artemisConnectedIds,
  isArtemisAuthenticated,
  isArtemisJunkCredential,
} from "@/provider/artemis-auth"
import type { Info as ProviderInfo } from "@/provider/provider"
import type { Info as AuthInfo } from "@/auth"

function provider(partial: Partial<ProviderInfo> & { id: string }): ProviderInfo {
  return {
    name: partial.name ?? partial.id,
    source: partial.source ?? "config",
    env: partial.env ?? [],
    options: partial.options ?? {},
    models: partial.models ?? { m: {} as ProviderInfo["models"][string] },
    ...partial,
  } as ProviderInfo
}

describe("artemis auth connected semantics", () => {
  test("without ARTEMIS every provider counts as connected", () => {
    delete process.env.ARTEMIS
    const providers = { anthropic: provider({ id: "anthropic", source: "config" }) }
    expect(artemisConnectedIds(providers, {})).toEqual(["anthropic"])
  })

  test("under ARTEMIS config-only providers are not connected", () => {
    process.env.ARTEMIS = "1"
    const providers = {
      cursor: provider({ id: "cursor", source: "config", env: ["CURSOR_API_KEY"] }),
      anthropic: provider({ id: "anthropic", source: "config", name: "Claude" }),
      openai: provider({ id: "openai", source: "config", name: "Codex" }),
    }
    expect(artemisConnectedIds(providers, {})).toEqual([])
    expect(Object.keys(artemisAuthenticatedProviders(providers, {}))).toEqual([])
    delete process.env.ARTEMIS
  })

  test("under ARTEMIS junk keys are not connected", () => {
    process.env.ARTEMIS = "1"
    expect(isArtemisJunkCredential("test")).toBe(true)
    expect(isArtemisJunkCredential("sk-ant-real-looking-key-here", "test")).toBe(true)
    expect(isArtemisJunkCredential("sk-ant-real-looking-key-here", "https://api.anthropic.com")).toBe(false)
    const providers = {
      anthropic: provider({ id: "anthropic", source: "config" }),
    }
    const auths: Record<string, AuthInfo> = {
      anthropic: { type: "api", key: "test", metadata: { baseURL: "test" } },
    }
    expect(isArtemisAuthenticated("anthropic", providers.anthropic, auths)).toBe(false)
    expect(artemisConnectedIds(providers, auths)).toEqual([])
    delete process.env.ARTEMIS
  })

  test("under ARTEMIS api auth or key marks connected", () => {
    process.env.ARTEMIS = "1"
    const providers = {
      anthropic: provider({ id: "anthropic", source: "config" }),
      cursor: provider({ id: "cursor", source: "api", key: "cursor_test_key_long" }),
    }
    const auths: Record<string, AuthInfo> = {
      anthropic: { type: "api", key: "sk-ant-test-key-long-enough" },
    }
    expect(isArtemisAuthenticated("anthropic", providers.anthropic, auths)).toBe(true)
    expect(artemisConnectedIds(providers, auths).sort()).toEqual(["anthropic", "cursor"])
    delete process.env.ARTEMIS
  })
})
