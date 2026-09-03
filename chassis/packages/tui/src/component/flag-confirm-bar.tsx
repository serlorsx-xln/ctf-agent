import { TextAttributes, type InputRenderable } from "@opentui/core"
import { createEffect, createMemo, createSignal, Show } from "solid-js"
import { useTheme } from "../context/theme"
import { useBindings } from "../keymap"
import { daemon } from "../artemis/client"
import { SplitBorder } from "../ui/border"

/** Inline main-page flag confirm (y/n) — replaces the centered modal for Artemis. */
export function FlagConfirmBar() {
  const { theme } = useTheme()
  const req = createMemo(() => daemon.flagConfirm[0]())
  // Rejecting opens a one-line "why" box. The operator already knows why the
  // candidate is wrong; without relaying it the solver only learns "no" and
  // starts doubting the checker instead of its own flag.
  const [asking, setAsking] = createSignal(false)
  const [reason, setReason] = createSignal("")
  let input: InputRenderable | undefined

  // New / replaced confirm must always start on y/n, not a leftover reject box.
  createEffect(() => {
    const id = req()?.request_id
    void id
    setAsking(false)
    setReason("")
  })

  function send(ok: boolean, why = "") {
    const r = req()
    if (!r) return
    setAsking(false)
    setReason("")
    void daemon.answerFlagConfirm(r.request_id, ok, why).catch(() => {})
  }

  function beginReject() {
    setReason("")
    setAsking(true)
  }

  // While the reason box is open, `y`/`n` must reach the input as text.
  useBindings(() => ({
    priority: 2,
    enabled: !asking(),
    bindings: req()
      ? [
          { key: "y", desc: "Accept flag", group: "Artemis", cmd: () => send(true) },
          { key: "n", desc: "Reject flag", group: "Artemis", cmd: () => beginReject() },
          // Esc must not reject — accidental Esc rejected golf flags mid-solve.
          { key: "escape", desc: "Ignore (use n to reject)", group: "Artemis", cmd: () => {} },
        ]
      : [],
  }))

  useBindings(() => ({
    priority: 2,
    enabled: asking(),
    bindings: req()
      ? [
          {
            key: "return",
            desc: "Reject with this reason",
            group: "Artemis",
            cmd: () => send(false, reason().trim()),
          },
          {
            key: "escape",
            desc: "Back to y/n",
            group: "Artemis",
            cmd: () => {
              setAsking(false)
              setReason("")
            },
          },
        ]
      : [],
  }))

  return (
    <Show when={req()}>
      {(r) => (
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
            <Show
              when={asking()}
              fallback={
                <>
                  <text attributes={TextAttributes.BOLD} fg={theme.warning}>
                    Confirm flag · y accept · n reject
                  </text>
                  <text fg={theme.textMuted}>Accept this candidate toward CORRECT?</text>
                </>
              }
            >
              <text attributes={TextAttributes.BOLD} fg={theme.warning}>
                Why is it wrong? · enter send · esc back
              </text>
              <text fg={theme.textMuted}>
                One line goes back to the agent — it steers the next attempt. Blank is fine.
              </text>
            </Show>
            <text fg={theme.success} wrapMode="char">
              {r().flag}
            </text>
            <Show when={asking()}>
              <input
                onInput={(e) => setReason(e)}
                focusedBackgroundColor={theme.backgroundElement}
                cursorColor={theme.primary}
                focusedTextColor={theme.text}
                ref={(node) => {
                  input = node
                  setTimeout(() => {
                    if (!input || input.isDestroyed) return
                    input.focus()
                  }, 1)
                }}
                placeholder="e.g. that's the placeholder in the source, not the real flag"
                placeholderColor={theme.textMuted}
              />
            </Show>
            <Show when={!asking()}>
              <box flexDirection="row" gap={2}>
                <box
                  paddingLeft={2}
                  paddingRight={2}
                  backgroundColor={theme.backgroundElement}
                  onMouseUp={() => beginReject()}
                >
                  <text fg={theme.text}>n reject</text>
                </box>
                <box paddingLeft={2} paddingRight={2} backgroundColor={theme.primary} onMouseUp={() => send(true)}>
                  <text fg={theme.selectedListItemText}>y accept</text>
                </box>
              </box>
            </Show>
          </box>
        </box>
      )}
    </Show>
  )
}
