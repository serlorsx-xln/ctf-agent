import { TextAttributes } from "@opentui/core"
import { createSignal } from "solid-js"
import { useTheme } from "../context/theme"
import { useDialog, type DialogContext } from "../ui/dialog"
import { useBindings } from "../keymap"
import { confirmCancelSetup } from "./dialog-confirm-restart"

export type DialogFlagsRequiredProps = {
  defaultValue?: number
  challengeName?: string
  /** When true, Esc cancels (null). When false, Esc uses default. */
  cancelable?: boolean
  onAnswer: (n: number | null) => void
  onDigitsChange?: (digits: string) => void
  initialDigits?: string
}

/** Digits-only prompt for flags_required — Enter submits, Esc cancels or uses default. */
export function DialogFlagsRequired(props: DialogFlagsRequiredProps) {
  const dialog = useDialog()
  const { theme } = useTheme()
  const fallback = Math.max(1, Math.min(64, props.defaultValue ?? 1))
  const [digits, setDigits] = createSignal(props.initialDigits ?? "")
  const cancelable = props.cancelable === true

  function display() {
    return digits() || String(fallback)
  }

  function setDigitsTracked(next: string | ((prev: string) => string)) {
    setDigits((prev) => {
      const value = typeof next === "function" ? next(prev) : next
      props.onDigitsChange?.(value)
      return value
    })
  }

  function submit(raw?: string) {
    const parsed = Number.parseInt((raw ?? display()).trim(), 10)
    const n = Number.isFinite(parsed) ? Math.max(1, Math.min(64, parsed)) : fallback
    props.onAnswer(n)
    dialog.clear()
  }

  async function onEscape() {
    if (cancelable) {
      const ok = await confirmCancelSetup(dialog)
      if (!ok) return
      props.onAnswer(null)
    } else {
      props.onAnswer(fallback)
    }
    dialog.clear()
  }

  useBindings(() => ({
    priority: 1,
    // When a nested confirm is open, let DialogProvider Esc dismiss it.
    enabled: dialog.stack.length <= 1,
    bindings: [
      {
        key: "escape",
        desc: cancelable ? "Confirm cancel" : `Use default (${fallback})`,
        group: "Dialog",
        cmd: () => void onEscape(),
      },
    ],
  }))

  useBindings(() => ({
    priority: 1,
    enabled: dialog.stack.length <= 1,
    bindings: [
      ...["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"].map((key) => ({
        key,
        desc: `Digit ${key}`,
        group: "Dialog",
        cmd: () => {
          setDigitsTracked((prev) => {
            const next = (prev + key).replace(/^0+(?=\d)/, "")
            return next.slice(0, 2)
          })
        },
      })),
      {
        key: "backspace",
        desc: "Delete digit",
        group: "Dialog",
        cmd: () => setDigitsTracked((prev) => prev.slice(0, -1)),
      },
      {
        key: "delete",
        desc: "Clear",
        group: "Dialog",
        cmd: () => setDigitsTracked(""),
      },
      {
        key: "return",
        desc: "Confirm",
        group: "Dialog",
        cmd: () => submit(),
      },
    ],
  }))

  return (
    <box paddingLeft={2} paddingRight={2} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text attributes={TextAttributes.BOLD} fg={theme.text}>
          Flags required
        </text>
        <text fg={theme.textMuted} onMouseUp={() => void onEscape()}>
          {cancelable ? "esc cancel?" : `esc → ${fallback}`}
        </text>
      </box>
      <box paddingBottom={1} gap={1}>
        <text fg={theme.textMuted}>
          How many distinct flags for{props.challengeName ? ` ${props.challengeName}` : " this challenge"}?
        </text>
        <text fg={theme.textMuted}>Digits only · Enter continues · default {fallback}</text>
        <box
          paddingLeft={2}
          paddingRight={2}
          paddingTop={1}
          paddingBottom={1}
          backgroundColor={theme.backgroundElement}
          flexDirection="row"
          justifyContent="center"
        >
          <text attributes={TextAttributes.BOLD} fg={theme.success}>
            {display()}
          </text>
        </box>
      </box>
      <box flexDirection="row" gap={2} paddingBottom={1} justifyContent="flex-end">
        <box
          paddingLeft={3}
          paddingRight={3}
          backgroundColor={theme.backgroundElement}
          onMouseUp={() => void onEscape()}
        >
          <text fg={theme.text}>{cancelable ? "esc cancel?" : "esc default"}</text>
        </box>
        <box paddingLeft={3} paddingRight={3} backgroundColor={theme.primary} onMouseUp={() => submit()}>
          <text fg={theme.selectedListItemText}>enter continue</text>
        </box>
      </box>
    </box>
  )
}

DialogFlagsRequired.show = (
  dialog: DialogContext,
  options?: { defaultValue?: number; challengeName?: string; cancelable?: boolean },
) => {
  const fallback = Math.max(1, Math.min(64, options?.defaultValue ?? 1))
  const cancelable = options?.cancelable === true
  const state = { digits: "" }
  return new Promise<number | null>((resolve) => {
    dialog.replace(
      () => (
        <DialogFlagsRequired
          defaultValue={options?.defaultValue}
          challengeName={options?.challengeName}
          cancelable={cancelable}
          initialDigits={state.digits}
          onDigitsChange={(digits) => {
            state.digits = digits
          }}
          onAnswer={(n) => resolve(n)}
        />
      ),
      () => resolve(cancelable ? null : fallback),
    )
  })
}
