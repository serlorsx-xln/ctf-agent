import { TextAttributes } from "@opentui/core"
import { For, Show, createSignal, onCleanup } from "solid-js"
import { useTheme } from "../context/theme"
import { useDialog, type DialogContext } from "../ui/dialog"
import { useBindings } from "../keymap"
import { daemon } from "../artemis/client"

/**
 * First-run sandbox install gate. Blocks challenge load/solve until Docker L0
 * + pack caches exist. Esc does nothing while installing; after ready, closes.
 */
function DialogSetupInstall(props: { onReady?: () => void }) {
  const dialog = useDialog()
  const { theme } = useTheme()
  const [busy, setBusy] = createSignal(daemon.setupInstalling[0]())
  const [err, setErr] = createSignal("")

  const ready = () => daemon.setupReady[0]() === true
  const message = () => daemon.setupMessage[0]()
  const logs = () => daemon.setupLogs[0]()

  async function startInstall() {
    if (busy() || ready()) return
    setErr("")
    setBusy(true)
    try {
      await daemon.startSetupInstall({ skipWarmRuntime: true })
      // Wait for setup_done, periodic ready re-probe, or daemon loss.
      // Never sit forever on a dead socket with a frozen last log line.
      const deadline = Date.now() + 60 * 60 * 1000
      let lastProbe = 0
      let lastLogLen = -1
      let lastLogAt = Date.now()
      while (Date.now() < deadline) {
        if (daemon.setupReady[0]() === true) break

        const n = daemon.setupLogs[0]().length
        if (n !== lastLogLen) {
          lastLogLen = n
          lastLogAt = Date.now()
        }

        // Terminal `artemis` setup (or a previous bake) may finish while this
        // dialog still thinks install is in flight — re-check every few seconds.
        if (Date.now() - lastProbe >= 4000) {
          lastProbe = Date.now()
          try {
            const ok = await daemon.refreshSetupStatus()
            if (ok) {
              daemon.setupInstalling[1](false)
              break
            }
          } catch {
            // probed below via stall path
          }
        }

        if (!daemon.setupInstalling[0]()) {
          const ok = await daemon.refreshSetupStatus()
          if (ok) break
          const lastLog = daemon.setupLogs[0]().at(-1) || ""
          if (/disconnected|interrupted/i.test(lastLog)) {
            setErr(
              "Lost Artemis daemon during install — quit and run `artemis` again (packs may already be ready).",
            )
          } else {
            setErr(daemon.setupMessage[0]() || "Install failed")
          }
          setBusy(false)
          return
        }

        // No new setup_log for 90s → confirm daemon still reachable.
        if (Date.now() - lastLogAt > 90_000) {
          try {
            await daemon.ensureConnected()
            const ok = await daemon.refreshSetupStatus()
            if (ok) {
              daemon.setupInstalling[1](false)
              break
            }
            // Docker build can be quiet for a while — keep waiting, but reset stall clock.
            lastLogAt = Date.now()
          } catch {
            setErr(
              "Lost Artemis daemon during install — quit and run `artemis` again (packs may already be ready).",
            )
            setBusy(false)
            daemon.setupInstalling[1](false)
            return
          }
        }

        await new Promise((r) => setTimeout(r, 500))
      }
      if (daemon.setupReady[0]() === true) {
        props.onReady?.()
        dialog.clear()
      } else {
        // Daemon may still be baking past the TUI wait window — do not clear
        // setupInstalling (Esc/retry while bake continues desyncs the gate).
        let stillInstalling = false
        try {
          const st = (await daemon.request<{ installing?: boolean }>("setup_status", {})) as {
            installing?: boolean
          }
          stillInstalling = st?.installing === true
        } catch {
          /* ignore */
        }
        setErr(
          stillInstalling
            ? "Install still running in the background — wait or restart Artemis later"
            : "Install timed out or failed — check Docker and retry",
        )
        setBusy(false)
        if (!stillInstalling) daemon.setupInstalling[1](false)
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      setBusy(false)
      daemon.setupInstalling[1](false)
    }
  }

  useBindings(() => ({
    bindings: [
      {
        key: "return",
        desc: "Start install or continue",
        group: "Dialog",
        cmd: () => {
          if (ready()) {
            props.onReady?.()
            dialog.clear()
            return
          }
          if (!busy()) void startInstall()
        },
      },
      {
        key: "escape",
        desc: busy() && !err() ? "Blocked while installing" : "Close / retry later",
        group: "Dialog",
        cmd: () => {
          // While a healthy install is running, keep Esc blocked.
          // After error / timeout / daemon loss, allow dismiss so the UI is not trapped.
          if (busy() && !err()) return
          dialog.clear()
        },
      },
    ],
  }))

  // Refresh status once when opened.
  void daemon.refreshSetupStatus().then((ok) => {
    if (ok) {
      props.onReady?.()
      dialog.clear()
    }
  })

  onCleanup(() => {
    // Abort in-flight install wait if the dialog is dismissed after an error.
    setBusy(false)
  })

  return (
    <box paddingLeft={2} paddingRight={2} gap={1} paddingBottom={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text attributes={TextAttributes.BOLD} fg={theme.text}>
          Install Artemis sandbox
        </text>
        <Show when={ready()}>
          <text fg={theme.textMuted}>ready</text>
        </Show>
      </box>
      <text fg={theme.textMuted} wrapMode="word">
        Docker L0 image + pack bake are required before solving. You cannot load a
        challenge until Install finishes. Prefer answering Yes to the terminal
        setup prompt when launching `artemis` (before this UI). For maximum solve
        speed later, run `artemis setup` once (adds warm runtime images).
      </text>
      <Show when={message()}>
        <text fg={theme.warning} wrapMode="word">
          {message()}
        </text>
      </Show>
      <Show when={err()}>
        <text fg={theme.error} wrapMode="word">
          {err()}
        </text>
      </Show>
      <box
        border
        borderColor={theme.border}
        height={10}
        paddingLeft={1}
        paddingRight={1}
        overflow="scroll"
      >
        <Show
          when={logs().length > 0}
          fallback={
            <text fg={theme.textMuted}>
              {busy() ? "Installing…" : "Press Enter to Install"}
            </text>
          }
        >
          <For each={logs().slice(-14)}>
            {(line) => (
              <text fg={theme.textMuted} wrapMode="none">
                {line.length > 90 ? `${line.slice(0, 87)}…` : line}
              </text>
            )}
          </For>
        </Show>
      </box>
      <Show when={busy() && logs().length > 0}>
        <text fg={theme.warning} wrapMode="word">
          {logs()[logs().length - 1] || "Installing…"}
        </text>
      </Show>
      <box flexDirection="row" justifyContent="flex-end" gap={1} paddingBottom={1}>
        <box
          paddingLeft={1}
          paddingRight={1}
          backgroundColor={busy() ? theme.backgroundElement : theme.primary}
          onMouseUp={() => {
            if (ready()) {
              props.onReady?.()
              dialog.clear()
              return
            }
            if (!busy()) void startInstall()
          }}
        >
          <text fg={theme.text}>
            {ready() ? "Continue" : busy() ? "Installing…" : "Install"}
          </text>
        </box>
      </box>
    </box>
  )
}

export const DialogSetupGate = {
  /** Single in-flight gate — boot + beforePromptSubmit must not stack dialogs. */
  _inflight: null as Promise<boolean> | null,

  async ensure(dialog: DialogContext): Promise<boolean> {
    if (daemon.setupReady[0]() === true) return true
    if (this._inflight) return this._inflight

    this._inflight = (async () => {
      try {
        const ok = await daemon.refreshSetupStatus()
        if (ok) return true
        return await new Promise<boolean>((resolve) => {
          let settled = false
          dialog.replace(
            () => (
              <DialogSetupInstall
                onReady={() => {
                  if (settled) return
                  settled = true
                  resolve(true)
                }}
              />
            ),
            () => {
              if (settled) return
              settled = true
              void daemon.refreshSetupStatus().then((ready) => resolve(ready))
            },
          )
          dialog.setSize("large")
        })
      } finally {
        this._inflight = null
      }
    })()

    return this._inflight
  },
}
