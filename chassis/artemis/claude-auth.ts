/**
 * Claude (/connect) auth methods — closer to Claude CLI options than a single API key paste.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { execFileSync } from "child_process"
import fs from "fs"
import os from "os"
import path from "path"

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
          return { baseURL: auth.metadata.baseURL }
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
              message: "Anthropic-compatible base URL",
              placeholder: "https://api.anthropic.com",
            },
            {
              type: "text",
              key: "key",
              message: "API key for that endpoint",
              placeholder: "sk-ant-...",
            },
            {
              type: "text",
              key: "model_id",
              message: "Custom model id (optional, e.g. claude-sonnet-4-6)",
              placeholder: "claude-sonnet-4-6",
            },
          ],
        },
      ],
    },
  }
}

export default {
  id: "artemis-claude-auth",
  server: ClaudeAuthPlugin,
}
