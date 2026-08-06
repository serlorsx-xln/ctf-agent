import type { TuiPluginApi } from "@opencode-ai/plugin/tui"
import { createMemo, For, type Accessor } from "solid-js"
import { useTheme } from "../../context/theme"
import { useCommandShortcut } from "../../keymap"

type TipPart = { text: string; highlight: boolean }
type TipShortcut = Accessor<string>
type Shortcuts = {
  modelList: TipShortcut
  sessionNew: TipShortcut
  sessionInterrupt: TipShortcut
  commandList: TipShortcut
  inputPaste: TipShortcut
}
type Tip = string | ((shortcuts: Shortcuts) => string | undefined)

function parseTip(tip: string): TipPart[] {
  const parts: TipPart[] = []
  const regex = /{highlight}(.*?){\/highlight}/g
  let lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = regex.exec(tip)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ text: tip.slice(lastIndex, match.index), highlight: false })
    }
    parts.push({ text: match[1], highlight: true })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < tip.length) {
    parts.push({ text: tip.slice(lastIndex), highlight: false })
  }
  return parts
}

function shortcutText(value: string) {
  return `{highlight}${value}{/highlight}`
}

function press(shortcut: string, action: string) {
  if (!shortcut) return undefined
  return `Press ${shortcutText(shortcut)} ${action}`
}

function TipView(props: { tip: string }) {
  const { theme } = useTheme()
  const parts = createMemo(() => parseTip(props.tip))
  return (
    <box flexDirection="row" maxWidth="100%">
      <text flexShrink={0} style={{ fg: theme.warning }}>
        ● Tip{" "}
      </text>
      <text flexShrink={1} wrapMode="word">
        <For each={parts()}>
          {(part) => <span style={{ fg: part.highlight ? theme.text : theme.textMuted }}>{part.text}</span>}
        </For>
      </text>
    </box>
  )
}

/** CTF-only tips — no coding-agent / OpenCode upsell noise. */
const TIPS: Tip[] = [
  "Paste a challenge path — Artemis loads it and runs the swarm.",
  "Flags required only when needed (default 1; HTB user+root → 2).",
  (s) => press(s.modelList(), "to pick solver models"),
  "Paste challenge text (or a folder path) — Artemis loads and solves.",
  (s) => press(s.sessionNew(), "for a fresh session"),
  (s) => press(s.sessionInterrupt(), "to stop mid-run"),
  "Add keys with /connect.",
  (s) => press(s.commandList(), "for actions"),
  (s) => press(s.inputPaste(), "to paste images/files"),
]

export function Tips(_props: { api?: TuiPluginApi; connected?: boolean }) {
  const shortcuts: Shortcuts = {
    modelList: useCommandShortcut("model.list"),
    sessionNew: useCommandShortcut("session.new"),
    sessionInterrupt: useCommandShortcut("session.interrupt"),
    commandList: useCommandShortcut("command.list"),
    inputPaste: useCommandShortcut("input.paste"),
  }
  const tip = createMemo(() => {
    const list = TIPS.map((t) => (typeof t === "function" ? t(shortcuts) : t)).filter(Boolean) as string[]
    if (!list.length) return ""
    const i = Math.floor(Date.now() / 60_000) % list.length
    return list[i]!
  })
  return tip() ? <TipView tip={tip()} /> : null
}
