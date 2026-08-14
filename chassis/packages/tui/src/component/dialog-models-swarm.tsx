import { TextAttributes, type InputRenderable, type ScrollBoxRenderable } from "@opentui/core"
import { createEffect, createMemo, createSignal, For, Show } from "solid-js"
import { entries, flatMap, groupBy, pipe } from "remeda"
import { useTerminalDimensions } from "@opentui/solid"
import { useLocal } from "../context/local"
import { useSync } from "../context/sync"
import { useTheme } from "../context/theme"
import { useDialog, type DialogContext } from "../ui/dialog"
import { useBindings } from "../keymap"
import { useConnected } from "./use-connected"
import { buildArtemisModelOptions } from "../util/artemis-model-options"
import { toRaceSpec } from "../util/artemis-models"
import { confirmCancelSetup } from "./dialog-confirm-restart"

export type DialogModelsSwarmProps = {
  preselected?: string[]
  onConfirm: (models: string[]) => void
  /** Keeps picks across remount when a nested confirm is declined. */
  onSelectedChange?: (models: string[]) => void
}

export function DialogModelsSwarm(props: DialogModelsSwarmProps) {
  const dialog = useDialog()
  const sync = useSync()
  const local = useLocal()
  const { theme } = useTheme()
  const connected = useConnected()
  const dimensions = useTerminalDimensions()
  const [query, setQuery] = createSignal("")
  const [selected, setSelected] = createSignal<string[]>([...(props.preselected ?? [])])
  const [index, setIndex] = createSignal(0)
  const MAX_AGENTS = 16
  let scroll: ScrollBoxRenderable | undefined
  let input: InputRenderable | undefined

  const options = createMemo(() =>
    buildArtemisModelOptions({
      sync,
      local,
      connected: connected(),
      connectedProviders: sync.data.provider_next?.connected ?? [],
      query: query(),
      preselected: selected(),
    }),
  )

  // Provider name arrives as `category` on every option, so the flat wall of
  // models can be grouped without any extra lookup.
  const grouped = createMemo<[string, ReturnType<typeof options>][]>(() =>
    pipe(
      options(),
      groupBy((opt) => opt.category ?? ""),
      entries(),
    ),
  )
  const flat = createMemo(() => pipe(grouped(), flatMap(([, opts]) => opts)))

  const rows = createMemo(
    () => grouped().reduce((acc, [category]) => acc + (category ? 1 : 0), 0) + flat().length,
  )
  const height = createMemo(() => Math.max(3, Math.min(rows(), Math.floor(dimensions().height / 2))))

  // Filtering shrinks the list under the cursor; keep it on a real row.
  createEffect(() => {
    const max = flat().length - 1
    if (index() > max) setIndex(Math.max(0, max))
  })

  /** Row offset of `index()`, counting the group headers rendered above it. */
  function rowOffset(target: number): number {
    let remaining = target
    let row = 0
    for (const [category, opts] of grouped()) {
      if (category) row++
      if (remaining < opts.length) return row + remaining
      row += opts.length
      remaining -= opts.length
    }
    return row
  }

  // Without this the cursor walks past the viewport and the rest of the list is
  // unreachable — the whole point of the scrollbox.
  createEffect(() => {
    const i = index()
    if (!scroll) return
    const target = scroll.getChildren()[rowOffset(i)]
    if (!target) return
    const y = target.y - scroll.y
    if (y >= scroll.height) scroll.scrollBy(y - scroll.height + 1)
    else if (y < 0) {
      scroll.scrollBy(y)
      if (i === 0) scroll.scrollTo(0)
    }
  })

  function currentSpec(): string | null {
    const opt = flat()[index()]
    if (!opt) return null
    return toRaceSpec(opt.value.providerID, opt.value.modelID)
  }

  function countOf(spec: string): number {
    return selected().reduce((n, s) => n + (s === spec ? 1 : 0), 0)
  }

  function addSpec(spec: string) {
    setSelected((prev) => (prev.length >= MAX_AGENTS ? prev : [...prev, spec]))
  }

  function removeSpec(spec: string) {
    setSelected((prev) => {
      const i = prev.lastIndexOf(spec)
      if (i < 0) return prev
      return [...prev.slice(0, i), ...prev.slice(i + 1)]
    })
  }

  function addCurrent() {
    const spec = currentSpec()
    if (spec) addSpec(spec)
  }

  function removeCurrent() {
    const spec = currentSpec()
    if (spec) removeSpec(spec)
  }

  createEffect(() => {
    props.onSelectedChange?.(selected())
  })

  function confirm() {
    const models = selected()
    if (models.length === 0) return
    props.onConfirm(models)
    dialog.clear()
  }

  async function cancel() {
    const ok = await confirmCancelSetup(dialog)
    if (!ok) return
    props.onConfirm([])
    dialog.clear()
  }

  useBindings(() => ({
    priority: 1,
    enabled: dialog.stack.length <= 1,
    bindings: [
      { key: "up", desc: "Previous model", group: "Dialog", cmd: () => setIndex((i) => Math.max(0, i - 1)) },
      {
        key: "down",
        desc: "Next model",
        group: "Dialog",
        cmd: () => setIndex((i) => Math.min(flat().length - 1, i + 1)),
      },
      { key: "tab", desc: "Add another of this model", group: "Dialog", cmd: () => addCurrent() },
      { key: "return", desc: "Confirm selection", group: "Dialog", cmd: () => confirm() },
      { key: "escape", desc: "Confirm cancel", group: "Dialog", cmd: () => void cancel() },
    ],
  }))

  // Space / backspace only while the search box is empty so they can type names.
  useBindings(() => ({
    priority: 2,
    enabled: query().length === 0 && dialog.stack.length <= 1,
    bindings: [
      { key: "space", desc: "Add another of this model", group: "Dialog", cmd: () => addCurrent() },
      { key: "backspace", desc: "Remove one of this model", group: "Dialog", cmd: () => removeCurrent() },
      { key: "-", desc: "Remove one of this model", group: "Dialog", cmd: () => removeCurrent() },
    ],
  }))

  return (
    <box paddingLeft={2} paddingRight={2} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text attributes={TextAttributes.BOLD} fg={theme.text}>
          Select swarm models
        </text>
        <text fg={theme.textMuted} onMouseUp={() => void cancel()}>
          esc cancel?
        </text>
      </box>
      <text fg={theme.textMuted}>
        Type to search · ↑↓ move · tab{query().length === 0 ? "/space" : ""} adds another · click/backspace removes one · enter confirms
      </text>
      <input
        onInput={(e) => setQuery(e)}
        onKeyDown={(e: { name?: string; preventDefault(): void }) => {
          if (query().length > 0) return
          if (e.name === "backspace" || e.name === "-") {
            e.preventDefault()
            removeCurrent()
          }
        }}
        focusedBackgroundColor={theme.backgroundPanel}
        cursorColor={theme.primary}
        focusedTextColor={theme.text}
        ref={(node) => {
          input = node
          setTimeout(() => {
            if (!input || input.isDestroyed) return
            input.focus()
          }, 1)
        }}
        placeholder="Search models"
        placeholderColor={theme.textMuted}
      />
      <Show
        when={flat().length > 0}
        fallback={
          <text fg={theme.textMuted}>
            {query()
              ? "No model matches that search"
              : "No authenticated models — run /connect first (Cursor / Claude / Codex / Gemini)"}
          </text>
        }
      >
        <scrollbox
          height={height()}
          scrollbarOptions={{ visible: true }}
          ref={(r: ScrollBoxRenderable) => (scroll = r)}
        >
          <For each={grouped()}>
            {([category, opts], groupIdx) => (
              <>
                <Show when={category}>
                  <text fg={theme.textMuted} attributes={TextAttributes.BOLD}>
                    {category}
                  </text>
                </Show>
                <For each={opts}>
                  {(opt, i) => {
                    const spec = () => toRaceSpec(opt.value.providerID, opt.value.modelID)
                    // Offset of this option within the flattened list.
                    const flatIndex = () =>
                      grouped()
                        .slice(0, groupIdx())
                        .reduce((acc, [, prev]) => acc + prev.length, 0) + i()
                    const active = () => flatIndex() === index()
                    const n = () => countOf(spec())
                    const picked = () => n() > 0
                    return (
                      <box
                        paddingLeft={1}
                        backgroundColor={active() ? theme.primary : undefined}
                        onMouseUp={() => {
                          const s = spec()
                          setIndex(flatIndex())
                          if (countOf(s) > 0) removeSpec(s)
                          else addSpec(s)
                        }}
                      >
                        <text fg={active() ? theme.selectedListItemText : picked() ? theme.success : theme.text}>
                          {picked() ? (n() > 1 ? `✓×${n()} ` : "✓ ") : "  "}
                          {opt.title}
                          {opt.footer ? ` · ${opt.footer}` : ""}
                        </text>
                      </box>
                    )
                  }}
                </For>
              </>
            )}
          </For>
        </scrollbox>
      </Show>
      <box flexDirection="row" gap={2} justifyContent="flex-end" paddingBottom={1}>
        <text fg={theme.textMuted}>
          {selected().length} agent{selected().length === 1 ? "" : "s"} · {flat().length} shown
        </text>
        <box paddingLeft={3} paddingRight={3} backgroundColor={theme.primary} onMouseUp={() => confirm()}>
          <text fg={theme.selectedListItemText}>enter confirm</text>
        </box>
      </box>
    </box>
  )
}

DialogModelsSwarm.show = (dialog: DialogContext, options?: { preselected?: string[] }) => {
  const state = { selected: [...(options?.preselected ?? [])] }
  return new Promise<string[]>((resolve) => {
    dialog.replace(
      () => (
        <DialogModelsSwarm
          preselected={state.selected}
          onSelectedChange={(models) => {
            state.selected = models
          }}
          onConfirm={(models) => resolve(models)}
        />
      ),
      () => resolve([]),
    )
  })
}
