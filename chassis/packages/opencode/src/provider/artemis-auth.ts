/** Artemis: a provider is "connected" only with real credentials — not config rename alone. */
import type { Info as AuthInfo } from "@/auth"
import type { Info as ProviderInfo } from "@/provider/provider"

const JUNK_KEYS = new Set([
  "test",
  "xxx",
  "asdf",
  "foo",
  "bar",
  "password",
  "apikey",
  "api_key",
  "changeme",
  "placeholder",
  "your_key",
  "your-key",
  "sk-ant-...",
  "cursor_...",
  "crsr_...",
])

/** Reject obvious placeholders so junk /connect entries don't skip onboarding. */
export function isArtemisJunkCredential(key: string | undefined, baseURL?: string): boolean {
  const k = (key || "").trim()
  if (!k || k.length < 8) return true
  if (JUNK_KEYS.has(k.toLowerCase())) return true
  if (baseURL) {
    const u = baseURL.trim()
    if (!/^https?:\/\//i.test(u)) return true
    try {
      // eslint-disable-next-line no-new
      new URL(u)
    } catch {
      return true
    }
  }
  return false
}

export function isArtemisAuthenticated(
  providerID: string,
  info: ProviderInfo,
  auths: Record<string, AuthInfo>,
): boolean {
  if (process.env.ARTEMIS !== "1") return true

  const stored = auths[providerID]
  if (stored?.type === "api") {
    const baseURL = stored.metadata?.baseURL
    if (!isArtemisJunkCredential(stored.key, baseURL)) return true
  }
  if (stored?.type === "oauth" && (stored.access || stored.refresh)) {
    if (stored.access && !isArtemisJunkCredential(stored.access)) return true
    if (stored.refresh) return true
  }
  if (stored?.type === "wellknown" && stored.key?.trim() && !isArtemisJunkCredential(stored.key)) return true

  if (info.key?.trim() && !isArtemisJunkCredential(info.key)) return true

  for (const envName of info.env ?? []) {
    const v = (process.env[envName] || "").trim()
    if (v && !isArtemisJunkCredential(v)) return true
  }
  return false
}

export function artemisConnectedIds(
  providers: Record<string, ProviderInfo>,
  auths: Record<string, AuthInfo>,
): string[] {
  if (process.env.ARTEMIS !== "1") return Object.keys(providers)
  return Object.entries(providers)
    .filter(([id, info]) => isArtemisAuthenticated(id, info, auths))
    .map(([id]) => id)
}

export function artemisAuthenticatedProviders(
  providers: Record<string, ProviderInfo>,
  auths: Record<string, AuthInfo>,
): Record<string, ProviderInfo> {
  if (process.env.ARTEMIS !== "1") return providers
  const out: Record<string, ProviderInfo> = {}
  for (const [id, info] of Object.entries(providers)) {
    if (isArtemisAuthenticated(id, info, auths)) out[id] = info
  }
  return out
}
