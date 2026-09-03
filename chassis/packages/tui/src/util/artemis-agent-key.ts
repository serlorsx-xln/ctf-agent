/** Shared swarm agent display keys — one source for live-log tags + roster labels.

 * Must stay aligned with backend ``model_id_from_spec`` / ``agent_display_key``.
 */

const AGENT_PROVIDERS = new Set([
  "cursor",
  "claude-sdk",
  "codex",
  "gemini-sdk",
  "anthropic",
  "openai",
  "google",
])

const EFFORT_SUFFIXES = new Set(["low", "medium", "high", "xhigh", "max"])

const RESERVED_AGENT_KEYS = new Set([
  "status",
  "boot",
  "artemis",
  "swarm",
  "race",
  "info",
  "debug",
  "warning",
  "error",
  "critical",
])

/**
 * Strip challenge / provider / effort from a live-log tag to the box label.
 * ``chal/claude-sdk/aliyuncs/glm-5.2 think`` → ``aliyuncs/glm-5.2``.
 */
export function shortAgent(tag: string): string {
  let t = tag.trim().replace(/\s+(think|ai|tool)$/i, "")
  const parts = t.split("/").filter(Boolean)
  if (parts.length < 2) return parts[0] || tag
  // live() tags ``{challenge}/{model_id}``; logging tags ``{challenge}/{spec}``.
  let rest = parts.slice(1)
  if (rest.length >= 2 && AGENT_PROVIDERS.has(rest[0]!)) rest = rest.slice(1)
  if (rest.length && EFFORT_SUFFIXES.has(rest[rest.length - 1]!)) rest = rest.slice(0, -1)
  return rest.join("/") || parts.at(-1) || tag
}

/** Display key aligned with backend ``model_id_from_spec``.

 * Everything after the provider, minus effort. Slashy Claude ids stay intact
 * (``claude-sdk/aliyuncs/glm-5.2`` → ``aliyuncs/glm-5.2``). Duplicate runners
 * keep ``#N`` from ``assign_runner_ids``.
 */
export function agentKeyFromSpec(spec: string): string {
  const s = spec.trim()
  if (!s) return "agent"
  const hash = s.match(/#(\d+)$/)
  const base = hash ? s.slice(0, s.lastIndexOf("#")) : s
  const parts = base.split("/").filter(Boolean)
  if (parts.length < 2) return hash ? `${base}#${hash[1]}` : s
  let rest = parts.slice(1)
  if (rest.length && EFFORT_SUFFIXES.has(rest[rest.length - 1]!)) {
    rest = rest.slice(0, -1)
  }
  const id = rest.join("/") || parts[1]!
  return hash ? `${id}#${hash[1]}` : id
}

/** Bracket tags that are log/metadata, never swarm agent keys. */
export function isReservedAgentTag(tag: string): boolean {
  return RESERVED_AGENT_KEYS.has(shortAgent(tag).toLowerCase())
}

export function isReservedAgentKey(key: string): boolean {
  return RESERVED_AGENT_KEYS.has(key.trim().toLowerCase())
}

/**
 * Whether ``claimer`` may take a note targeted at ``noteTarget``.
 * Mirrors backend ``operator_inbox.targets_match`` (base ↔ ``base#N``).
 */
export function targetsMatch(noteTarget: string | null | undefined, claimer: string | null | undefined): boolean {
  if (noteTarget == null || noteTarget === "") return true
  if (!claimer) return false
  if (noteTarget === claimer) return true
  return claimer.startsWith(`${noteTarget}#`) || noteTarget.startsWith(`${claimer}#`)
}
