import type { TuiPlugin, TuiPluginApi } from "@opencode-ai/plugin/tui"
import type { BuiltinTuiPlugin } from "../builtins"
import { createMemo, Show } from "solid-js"
import { daemon } from "../../artemis/client"

const id = "internal:sidebar-context"

const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
})

function fmtTokens(n: number): string {
  return n.toLocaleString()
}

/** Provider-reported swarm usage only — never invent % or dollar estimates. */
function View(_props: { api: TuiPluginApi; session_id: string }) {
  const theme = () => _props.api.theme.current
  const usage = daemon.usage[0]
  const running = daemon.swarmRunning[0]

  const state = createMemo(() => {
    const au = usage()
    if (!au) return null
    // Official totals only — hide until the provider has reported tokens.
    if (au.tokens <= 0 && au.input <= 0 && au.output <= 0) return null
    return au
  })

  return (
    <box>
      <text fg={theme().text}>
        <b>Context</b>
      </text>
      <Show
        when={state()}
        fallback={
          <>
            <text fg={theme().textMuted}>
              {running() ? "waiting for provider usage…" : "tokens not reported"}
            </text>
            <text fg={theme().textMuted}>— spent</text>
          </>
        }
      >
        {(s) => {
          const cost = s().cost_usd
          const parts = [
            s().input > 0 ? `${fmtTokens(s().input)} in` : null,
            s().cache_read > 0 ? `${fmtTokens(s().cache_read)} cache` : null,
            s().output > 0 ? `${fmtTokens(s().output)} out` : null,
          ].filter(Boolean)
          return (
            <>
              <text fg={theme().textMuted}>
                {parts.length ? parts.join(" · ") : `${fmtTokens(s().tokens)} tokens`}
              </text>
              <text fg={theme().textMuted}>{fmtTokens(s().tokens)} total</text>
              <text fg={theme().textMuted}>
                {cost != null ? `${money.format(cost)} spent (reported)` : "— spent (not reported)"}
              </text>
            </>
          )
        }}
      </Show>
    </box>
  )
}

const tui: TuiPlugin = async (api) => {
  api.slots.register({
    order: 100,
    slots: {
      sidebar_content(_ctx, props) {
        return <View api={api} session_id={props.session_id} />
      },
    },
  })
}

const plugin: BuiltinTuiPlugin = {
  id,
  tui,
}

export default plugin
