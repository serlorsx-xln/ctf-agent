import { For, Show, createMemo } from "solid-js"
import { useTheme } from "../context/theme"
import { SplitBorder } from "../ui/border"
import { daemon } from "../artemis/client"
import { isSolverHoldActive, pendingOperatorQueue } from "../util/artemis-live-log"
import { Spinner } from "./spinner"

function preview(text: string, max = 72): string {
  const t = text.replace(/\s+/g, " ").trim()
  return t.length <= max ? t : t.slice(0, max - 1) + "…"
}

/**
 * Sticky panel for mid-solve Queue delivery — mirrors SwarmAgentFooter /
 * FlagConfirmBar so pending notes stay pinned above the prompt (not scroll-away feed text).
 * Hidden during Hold Q&A (those sends are steer-to-winner). Still shown in the
 * Writeup window where notes are parked as queue until Hold drains them.
 */
export function OperatorQueueBar() {
  const { theme } = useTheme()
  const holding = createMemo(() => isSolverHoldActive(daemon.swarmEvents[0]()))
  const items = createMemo(() => {
    if (process.env.ARTEMIS !== "1") return [] as string[]
    if (!daemon.swarmRunning[0]()) return [] as string[]
    if (holding()) return [] as string[]
    return pendingOperatorQueue(daemon.swarmEvents[0](), daemon.swarmFocus[0]())
  })
  const count = createMemo(() => items().length)
  const scopeLabel = createMemo(() => {
    const f = daemon.swarmFocus[0]()
    return f ? f : "all agents"
  })

  return (
    <Show when={count() > 0}>
      <box flexShrink={0} marginTop={1}>
        <box
          paddingTop={1}
          paddingBottom={1}
          paddingLeft={2}
          paddingRight={2}
          {...SplitBorder}
          border={["left"]}
          borderColor={theme.warning}
          backgroundColor={theme.backgroundPanel}
          gap={1}
        >
          <box flexDirection="row" justifyContent="space-between" gap={1}>
            <box flexDirection="row" gap={1}>
              <Spinner color={theme.warning}>Queued</Spinner>
              <text fg={theme.textMuted}>
                · {count()} pending · {scopeLabel()} · waits for idle / turn boundary
              </text>
            </box>
          </box>
          <For each={items()}>
            {(text, index) => (
              <box flexDirection="row" gap={1}>
                <text fg={theme.warning}>{index() + 1}.</text>
                <text fg={theme.text} wrapMode="word">
                  {preview(text)}
                </text>
              </box>
            )}
          </For>
          <text fg={theme.textMuted}>
            Solving continues — delivered when each targeted turn goes idle
          </text>
        </box>
      </box>
    </Show>
  )
}
