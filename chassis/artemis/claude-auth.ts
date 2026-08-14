/**
 * Claude (/connect) auth methods — closer to Claude CLI options than a single API key paste.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { execFileSync } from "child_process"
import fs from "fs"
import os from "os"
import path from "path"

const NON_CHAT_RE = /whisper|tts|orpheus|transcri|speech|audio|embedding|moderation|dall-e|tts-|realtime/i

/** Same rules as Claude CLI: keep /anthropic, strip a trailing /v1 or /v1/models. */
export function normalizeAnthropicBaseUrl(url: string): string {
  const u = (url || "").trim().replace(/\/+$/, "")
  if (!u) return ""
  const low = u.toLowerCase()
  if (low.endsWith("/v1/models")) return u.slice(0, -"/v1/models".length).replace(/\/+$/, "")
  if (low.endsWith("/v1")) return u.slice(0, -"/v1".length).replace(/\/+$/, "")
  return u
}

/**
 * OpenCode / AI SDK join ``{baseURL}/messages``.
 * Claude CLI joins ``{ANTHROPIC_BASE_URL}/v1/messages``.
 * So the TUI loader must pass the CLI root + ``/v1``.
 */
export function anthropicSdkBaseUrl(url: string): string {
  const root = normalizeAnthropicBaseUrl(url)
  if (!root) return ""
  return `${root}/v1`
}

/** Operator-supplied catalog URL → GET endpoint. */
export function normalizeModelsListUrl(url: string): string {
  const u = (url || "").trim().replace(/\/+$/, "")
  if (!u) return ""
  const low = u.toLowerCase()
  if (low.endsWith("/v1/models") || low.endsWith("/models")) return u
  if (low.endsWith("/v1")) return `${u}/models`
  return `${u}/v1/models`
}

/** Optional catalog: same host as ANTHROPIC_BASE_URL, OpenAI GET /v1/models. */
export function siblingOpenaiModelsUrl(anthropicBase: string): string {
  const root = normalizeAnthropicBaseUrl(anthropicBase)
  if (!root || /api\.anthropic\.com/i.test(root)) return ""
  try {
    return `${new URL(root).origin}/v1/models`
  } catch {
    return ""
  }
}

/** Prefer the pasted models URL; otherwise try the chat host's /v1/models. */
export function resolveModelsListUrl(modelsUrl: string, anthropicBase: string): string {
  return normalizeModelsListUrl(modelsUrl) || siblingOpenaiModelsUrl(anthropicBase)
}

async function fetchModelIds(listUrl: string, key: string): Promise<string[]> {
  if (!listUrl) return []
  const res = await fetch(listUrl, {
    headers: { Authorization: `Bearer ${key}` },
    signal: AbortSignal.timeout(15_000),
  })
  if (!res.ok) return []
  const body = (await res.json()) as { data?: unknown }
  const rows = Array.isArray(body?.data) ? body.data : Array.isArray(body) ? body : []
  const ids: string[] = []
  for (const row of rows) {
    const id = typeof row === "string" ? row : (row as { id?: unknown })?.id
    if (typeof id === "string" && id.trim() && !NON_CHAT_RE.test(id)) ids.push(id.trim())
  }
  return ids
}

function readClaudeCodeApiKey(): string | undefined {
  // 1) macOS Keychain (Claude Code)
  if (process.platform === "darwin") {
    try {
      const raw = execFileSync(
        "security",
        ["find-generic-password", "-s", "Claude Code-credentials", "-w"],
        { encoding: "utf8", timeout: 3000 },
      ).trim()
      if (raw) {
        try {
          const parsed = JSON.parse(raw) as Record<string, unknown>
          const key =
            (parsed.apiKey as string) ||
            (parsed.api_key as string) ||
            (parsed.primaryApiKey as string) ||
            ""
          if (key.trim()) return key.trim()
          const oauth = parsed.claudeAiOauth as { accessToken?: string } | undefined
          const access = (parsed.accessToken as string) || oauth?.accessToken
          if (access?.trim()) return access.trim()
        } catch {
          if (raw.startsWith("sk-ant-") || raw.startsWith("sk-")) return raw
        }
      }
    } catch {
      /* no keychain item */
    }
  }

  // 2) Common Claude Code credential files
  const candidates = [
    path.join(os.homedir(), ".claude", ".credentials.json"),
    path.join(os.homedir(), ".config", "claude", ".credentials.json"),
    path.join(os.homedir(), "Library", "Application Support", "Claude", "credentials.json"),
  ]
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue
      const parsed = JSON.parse(fs.readFileSync(file, "utf8")) as Record<string, unknown>
      const key =
        (parsed.apiKey as string) ||
        (parsed.api_key as string) ||
        (parsed.primaryApiKey as string) ||
        ""
      if (key.trim()) return key.trim()
      const oauth = parsed.claudeAiOauth as { accessToken?: string } | undefined
      const access = (parsed.accessToken as string) || oauth?.accessToken
      if (access?.trim()) return access.trim()
    } catch {
      /* ignore */
    }
  }
  return undefined
}

export const ClaudeAuthPlugin: Plugin = async () => {
  return {
    auth: {
      provider: "anthropic",
      async loader(getAuth) {
        const auth = await getAuth()
        if (auth.type === "api" && auth.metadata?.baseURL) {
          return { baseURL: anthropicSdkBaseUrl(auth.metadata.baseURL) }
        }
        return {}
      },
      methods: [
        {
          type: "api",
          label: "Anthropic API key",
          prompts: [
            {
              type: "text",
              key: "key",
              message: "Anthropic API key (console.anthropic.com → API keys)",
              placeholder: "sk-ant-...",
            },
          ],
        },
        {
          type: "api",
          label: "Claude setup-token (Claude Code CLI)",
          prompts: [
            {
              type: "text",
              key: "key",
              message: "Paste token from: claude setup-token",
              placeholder: "sk-ant-... or setup token",
            },
          ],
        },
        {
          // oauth+auto so TUI AutoMethod runs callback (api.authorize is unused by TUI)
          type: "oauth",
          label: "Import from Claude Code (local)",
          authorize: async () => ({
            url: "https://claude.ai/code",
            instructions:
              "Importing credentials from Claude Code on this machine (keychain / ~/.claude). No browser login needed.",
            method: "auto" as const,
            callback: async () => {
              const key = readClaudeCodeApiKey()
              if (!key) return { type: "failed" as const }
              return {
                type: "success" as const,
                key,
                metadata: { source: "claude-code" },
              }
            },
          }),
        },
        {
          type: "api",
          label: "Custom base URL + API key",
          prompts: [
            {
              type: "text",
              key: "baseURL",
              message: "Target API URL (ANTHROPIC_BASE_URL — where chat goes)",
              placeholder: "https://your-host/anthropic",
              validate: (value: string) => {
                const v = value.trim()
                if (!/^https?:\/\//i.test(v)) return "Must start with http:// or https://"
                return undefined
              },
            },
            {
              type: "text",
              key: "key",
              message: "API key (Bearer)",
              placeholder: "sk-...",
            },
            {
              type: "text",
              key: "modelsURL",
              message: "Models list URL to fetch ids (optional — skip and pick/type a model later)",
              placeholder: "https://your-host/v1/models",
              validate: (value: string) => {
                const v = value.trim()
                if (!v) return undefined
                if (!/^https?:\/\//i.test(v)) return "Must start with http:// or https://"
                return undefined
              },
            },
            {
              type: "text",
              key: "model_id",
              message: "Or pin one model id now (optional — you can pick from the list later)",
              placeholder: "bigmodel/glm-5.2",
            },
          ],
        },
      ],
    },

    provider: {
      id: "anthropic",
      async models(provider, ctx) {
        const official = { ...provider.models } as typeof provider.models
        const key = ctx.auth?.type === "api" ? ctx.auth.key : ""
        const meta = ctx.auth?.type === "api" ? ctx.auth.metadata : undefined
        const extra = (meta?.model_id || process.env.ANTHROPIC_MODEL_ID || "").trim()
        const baseURL = meta?.baseURL ? normalizeAnthropicBaseUrl(meta.baseURL) : ""
        const listUrl = resolveModelsListUrl(meta?.modelsURL || "", baseURL)
        const custom = Boolean(baseURL && !/api\.anthropic\.com/i.test(baseURL))
        const template = (Object.values(official)[0] ?? {}) as Record<string, unknown>
        const templateApi = (template as { api?: Record<string, unknown> }).api
        // Custom ANTHROPIC_BASE_URL: only the operator's catalog / typed id.
        // Official Claude ids would be sent to their gateway with the wrong api.id.
        const out = (custom ? {} : official) as typeof provider.models

        const add = (id: string, name?: string) => {
          if (!id || out[id]) return
          out[id] = {
            ...template,
            id,
            name: name ?? id,
            api: { ...templateApi, id },
            status: "active",
            release_date: "2099-01-01",
          } as (typeof provider.models)[string]
        }

        if (key && listUrl) {
          try {
            for (const id of await fetchModelIds(listUrl, key)) add(id)
          } catch {
            /* catalog optional — pick/type a model later */
          }
        }
        if (extra) add(extra, `${extra} (custom)`)
        return out
      },
    },
  }
}

export default {
  id: "artemis-claude-auth",
  server: ClaudeAuthPlugin,
}
