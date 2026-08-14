/** Artemis providers: Cursor + Claude + Codex + Gemini. */
export const ARTEMIS_CHAT_PROVIDERS = new Set(["cursor", "anthropic", "openai", "google"])

const ARTEMIS_PROVIDER_NAMES: Record<string, string> = {
  cursor: "Cursor",
  anthropic: "Claude",
  openai: "Codex",
  google: "Gemini",
}

const NON_CHAT_RE = /whisper|tts|orpheus|transcri|speech|audio|embedding|moderation|dall-e|tts-|realtime/i

export type ArtemisModelValue = { providerID: string; modelID: string }

const TYPED_ID_PROVIDERS = ["anthropic"] as const

/** Type any model id for Claude (custom ANTHROPIC_BASE_URL). */
export function typedConnectModelOptions(
  needle: string,
  connectedProviders: string[],
  existing: ArtemisModelValue[],
  providerID?: string,
): ArtemisModelValue[] {
  const id = needle.trim()
  if (!id || /\s/.test(id) || id.length < 2) return []
  const out: ArtemisModelValue[] = []
  for (const pid of TYPED_ID_PROVIDERS) {
    if (providerID && pid !== providerID) continue
    if (!connectedProviders.includes(pid)) continue
    if (existing.some((item) => item.providerID === pid && item.modelID === id)) continue
    out.push({ providerID: pid, modelID: id })
  }
  return out
}

export function artemisProviderName(providerID: string, fallback?: string): string {
  return ARTEMIS_PROVIDER_NAMES[providerID] ?? fallback ?? providerID
}

export function isArtemisChatModel(input: {
  providerID: string
  modelID?: string
  name?: string
  modalities?: { input?: string[]; output?: string[] }
}): boolean {
  if (process.env.ARTEMIS !== "1") return true
  if (!ARTEMIS_CHAT_PROVIDERS.has(input.providerID)) return false

  const id = `${input.modelID ?? ""} ${input.name ?? ""}`
  if (NON_CHAT_RE.test(id)) return false

  const out = input.modalities?.output
  if (out && out.length > 0 && !out.includes("text")) return false

  return true
}

/** Footer badge in model picker (human UI). */
export function artemisModelFooter(providerID: string): string | undefined {
  if (process.env.ARTEMIS !== "1") return undefined
  if (!ARTEMIS_CHAT_PROVIDERS.has(providerID)) return undefined
  return "CTF"
}

/** Race spec for swarm_start (cursor/, claude-sdk/, codex/, gemini-sdk/). */
export function toRaceSpec(providerID: string, modelID: string): string {
  if (providerID === "anthropic") return `claude-sdk/${modelID}`
  if (providerID === "openai") return `codex/${modelID}`
  if (providerID === "google") return `gemini-sdk/${modelID}`
  return `cursor/${modelID}`
}

const EFFORT_SUFFIX = /^(low|medium|high|xhigh|max)$/

/** Parse a race spec back into provider + model ids (strips effort suffix). */
export function fromRaceSpec(spec: string): { providerID: string; modelID: string } | null {
  const slash = spec.indexOf("/")
  if (slash <= 0) return null
  const prefix = spec.slice(0, slash)
  let rest = spec.slice(slash + 1)
  if (!rest) return null
  // Effort lives after the model id: claude-sdk/claude-opus-4-6/max
  const lastSlash = rest.lastIndexOf("/")
  if (lastSlash > 0) {
    const maybeEffort = rest.slice(lastSlash + 1)
    if (EFFORT_SUFFIX.test(maybeEffort)) {
      rest = rest.slice(0, lastSlash)
    }
  }
  const modelID = rest
  if (!modelID) return null
  if (prefix === "claude-sdk" || prefix === "anthropic" || prefix === "claude")
    return { providerID: "anthropic", modelID }
  if (prefix === "codex" || prefix === "openai") return { providerID: "openai", modelID }
  if (prefix === "gemini-sdk" || prefix === "google" || prefix === "gemini")
    return { providerID: "google", modelID }
  if (prefix === "cursor") return { providerID: "cursor", modelID }
  return null
}
