import type { TuiPlugin, TuiPluginApi } from "@opencode-ai/plugin/tui"
import type { BuiltinTuiPlugin } from "../builtins"
import { createMemo, Show } from "solid-js"
import { daemon } from "../../artemis/client"

const id = "internal:sidebar-challenge"

function View(props: { api: TuiPluginApi }) {
  const theme = () => props.api.theme.current
  const st = daemon.sessionState[0]

  const name = createMemo(() => st().challenge_name || "")
  const required = createMemo(() => Math.max(1, Number(st().flags_required) || 1))
  const accepted = createMemo(() => (st().accepted_flags || []).length)
  const loaded = createMemo(() => Boolean(st().challenge_dir))

  return (
    <box gap={1}>
      <text fg={theme().text}>
        <b>Challenge</b>
      </text>
      <Show
        when={loaded()}
        fallback={<text fg={theme().textMuted}>Paste a challenge to load</text>}
      >
        <text fg={theme().text}>{name() || "loaded"}</text>
        <text fg={theme().textMuted}>
          {accepted()}/{required()} flags
        </text>
      </Show>
    </box>
  )
}

const tui: TuiPlugin = async (api) => {
  api.slots.register({
    order: 50,
    slots: {
      sidebar_content() {
        return <View api={api} />
      },
    },
  })
}

const plugin: BuiltinTuiPlugin = {
  id,
  tui,
}

export default plugin
