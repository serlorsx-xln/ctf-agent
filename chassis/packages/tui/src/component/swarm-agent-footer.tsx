import { For, Show, createEffect, createMemo, createSignal, onCleanup } from "solid-js"
import { TextAttributes } from "@opentui/core"
import { useTheme } from "../context/theme"
import { useBindings } from "../keymap"
import { SplitBorder } from "../ui/border"
import { daemon } from "../artemis/client"
import { listSwarmAgents } from "../util/artemis-swarm-agents"
import { hasAgentActivity, hasCorrectSolveOutcome, hasTerminalSolveOutcome, isSolverHoldActive } from "../util/artemis-live-log"
import { formatSwarmElapsed } from "../util/format"
import { Spinner } from "./spinner"
import { useToast } from "../ui/toast"
import { useDialog } from "../ui/dialog"
import { confirmStopWork } from "./dialog-confirm-restart"

type NavItem = { id: string | null; label: string }

/**
 * Sticky swarm status above the prompt (always visible while a run is active).
 *
 * Multi-agent: Claude-style page navigator under the status line.
 * Esc cascade (only bound when actionable — never steals session.interrupt):
 *   1) leave select mode → 2) back to main page → 3) stop while process alive
 * Chat on main (all agents) and on agent pages (that agent only).
 */
export function SwarmAgentFooter() {
  const { theme } = useTheme()
  const toast = useToast()
  const dialog = useDialog()
  const [hover, setHover] = createSignal<string | null>(null)

  const dialogOpen = createMemo(() => dialog.stack.length > 0)
  const events = createMemo(() => daemon.swarmEvents[0]())
  const agents = createMemo(() =>
    listSwarmAgents(events(), daemon.lastModels[0](), daemon.swarmRoster[0]()),
  )
  const multi = createMemo(() => agents().length > 1)
  /** Process still alive (may outlive Solving UI after quota/CORRECT). */
  const canStop = createMemo(() => daemon.swarmRunning[0]())
  const startedAt = createMemo(() => daemon.swarmStartedAt[0]())
  const endedAt = createMemo(() => daemon.swarmEndedAt[0]())
  const terminalDone = createMemo(
    () => hasTerminalSolveOutcome(events(), multi()) || daemon.flowCompleted[0](),
  )
  const agentStarted = createMemo(() => hasAgentActivity(events()))
  /** Sticky status for single- and multi-agent — lives next to the prompt. */
  const showStatus = createMemo(
    () =>
      process.env.ARTEMIS === "1" &&
      !dialogOpen() &&
      (canStop() || (startedAt() != null && !terminalDone())),
  )
  const showNav = createMemo(
    () =>
      showStatus() &&
      multi() &&
      (canStop() ||
        daemon.swarmRoster[0]().length > 0 ||
        events().length > 0),
  )
  const visible = createMemo(() => showStatus())
  const solving = createMemo(() => canStop() && !terminalDone())

  const [now, setNow] = createSignal(Date.now())
  createEffect(() => {
    if (!canStop()) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(id))
  })
  const elapsedLabel = createMemo(() =>
    formatSwarmElapsed(startedAt(), endedAt(), now(), solving()),
  )
  const holding = createMemo(
    () => canStop() && terminalDone() && isSolverHoldActive(events()),
  )
  const answering = createMemo(() => {
    if (!holding()) return false
    // Spinner stays up for the whole Hold turn: Answering… until the next
    // qa_sep (or Hold released). Multi-line Q&A must not clear it on chunk 1.
    let lastAnswer = -1
    let lastSep = -1
    const list = events()
    for (let i = 0; i < list.length; i++) {
      const ev = list[i]!
      if (ev.kind === "qa_sep") {
        lastSep = i
        continue
      }
      if (ev.kind !== "status") continue
      const t = String(ev.text || "")
      if (/^Hold released\b/i.test(t)) lastSep = i
      if (/^Answering/i.test(t)) lastAnswer = i
    }
    return lastAnswer > lastSep
  })
  const statusLabel = createMemo(() => {
    if (solving()) return agentStarted() ? "Solving" : "Starting…"
    if (holding()) return answering() ? "Hold · answering…" : "Hold · Q&A"
    if (
      canStop() &&
      hasCorrectSolveOutcome(events()) &&
      !events().some(
        (ev) =>
          ev.kind === "status" &&
          /^Hold\b/i.test(String(ev.text || "")) &&
          !/^Hold released\b/i.test(String(ev.text || "")),
      )
    ) {
      return "Writeup…"
    }
    if (canStop()) return "Stopping…"
    return "Solve"
  })
  const spinStatus = createMemo(
    () => solving() || answering() || statusLabel() === "Writeup…",
  )

  const items = createMemo<NavItem[]>(() => [
    { id: null, label: "main" },
    ...agents().map((a) => ({ id: a, label: a })),
  ])
  const focus = createMemo(() => daemon.swarmFocus[0]())
  const armed = createMemo(() => daemon.swarmNavArmed[0]())
  const cursor = createMemo(() => {
    const c = daemon.swarmNavCursor[0]()
    const n = items().length
    if (n === 0) return 0
    return Math.max(0, Math.min(c, n - 1))
  })
  const pageLabel = createMemo(() => {
    const f = focus()
    if (f == null) return "main"
    const i = agents().indexOf(f)
    return i >= 0 ? `${f} (${i + 1}/${agents().length})` : f
  })
  const navActive = createMemo(
    () => showNav() && !daemon.flagConfirm[0]() && (armed() || focus() != null),
  )
  const escActive = createMemo(() => armed() || focus() != null || canStop())

  function move(delta: number) {
    const n = items().length
    if (n === 0) return
    const next = (cursor() + delta + n) % n
    daemon.setSwarmNavCursor(next)
  }
  function openSelected() {
    const i = cursor()
    const item = items()[i]
    if (!item) return
    daemon.setSwarmNavCursor(i)
    daemon.setSwarmFocus(item.id)
  }
  function openAt(index: number) {
    daemon.setSwarmNavCursor(index)
    const item = items()[index]
    if (!item) return
    daemon.setSwarmFocus(item.id)
  }

  /** After CORRECT/quota the process may linger in sandbox cleanup — Esc should
   * force-stop without another confirm dialog (operator already finished). */
  const forceStopWithoutConfirm = createMemo(() => canStop() && terminalDone())

  let stopForceTimer: ReturnType<typeof setTimeout> | undefined
  /** Monotonic id so unlock timers never clear a newer swarm run. */
  let unlockRunId = 0
  function clearStopForceTimer() {
    if (stopForceTimer != null) {
      clearTimeout(stopForceTimer)
      stopForceTimer = undefined
    }
  }
  onCleanup(() => clearStopForceTimer())

  function unlockSwarmUiLocally(
    message: string,
    runId: number,
    opts?: { clearRunning?: boolean },
  ) {
    clearStopForceTimer()
    if (runId !== unlockRunId) return
    if (!daemon.swarmRunning[0]()) return
    // Post-CORRECT cleanup hang: keep swarmRunning so Esc / /stop still work.
    // Manual stop fallback / stop-failed: clear local running so UI is not stuck.
    if (opts?.clearRunning !== false) {
      daemon.swarmRunning[1](false)
    }
    daemon.setSolveLocked(false)
    daemon.setSuppressSolveGate(true)
    daemon.setSwarmNavArmed(false)
    daemon.setSwarmFocus(null)
    if (daemon.swarmEndedAt[0]() == null) daemon.swarmEndedAt[1](Date.now())
    toast.show({ message, variant: "warning" })
  }

  // After a finished solve, if cleanup hangs: unlock chat, then abandon Stopping.
  // Never force-stop after CORRECT — writeup (≤90s) and Q&A hold stay intentional.
  createEffect(() => {
    if (!(canStop() && terminalDone())) {
      clearStopForceTimer()
      return
    }
    if (holding() || hasCorrectSolveOutcome(events())) {
      clearStopForceTimer()
      const runId = ++unlockRunId
      // Unlock chat quickly so the operator can ask during writeup/hold.
      stopForceTimer = setTimeout(() => {
        unlockSwarmUiLocally(
          "Ask follow-ups — Esc or /stop ends the session",
          runId,
          { clearRunning: false },
        )
      }, 1500)
      onCleanup(() => clearStopForceTimer())
      return
    }
    clearStopForceTimer()
    const runId = ++unlockRunId
    stopForceTimer = setTimeout(() => {
      unlockSwarmUiLocally(
        "Cleanup still running — chat unlocked (esc/stop still work)",
        runId,
        { clearRunning: false },
      )
      // Hung Cursor writeup / missed swarm_exit left Stopping for 1h+. After a
      // grace period, clear local running and ask daemon to stop for real.
      stopForceTimer = setTimeout(() => {
        void daemon.reconcileSwarmRunning().then((still) => {
          if (runId !== unlockRunId) return
          if (!still) return
          void daemon.request("swarm_stop", {}).catch(() => {})
          unlockSwarmUiLocally("Stopped — post-solve cleanup abandoned", runId, {
            clearRunning: true,
          })
        })
      }, 25000)
    }, 4000)
    onCleanup(() => clearStopForceTimer())
  })

  async function requestStopSwarm(opts?: { force?: boolean }) {
    const force = opts?.force === true || forceStopWithoutConfirm()
    if (!force) {
      const ok = await confirmStopWork(dialog)
      if (!ok) return
    }
    try {
      clearStopForceTimer()
      // Keep timer armed until swarm_exit — do not clear on RPC ack (cleanup can hang).
      const runId = ++unlockRunId
      stopForceTimer = setTimeout(() => {
        unlockSwarmUiLocally("Stopped locally — cleanup still running in background", runId)
      }, 8000)
      await daemon.request("swarm_stop", {})
      daemon.setSwarmNavArmed(false)
      daemon.setSwarmFocus(null)
      toast.show({
        message: force ? "Force-stopping…" : "Stopping swarm…",
        variant: "info",
      })
    } catch (e) {
      unlockSwarmUiLocally(
        `Stop failed: ${e instanceof Error ? e.message : String(e)}`,
        unlockRunId,
      )
    }
  }

  function armHint(): string {
    if (!showNav()) {
      if (!canStop()) return ""
      return forceStopWithoutConfirm() ? "esc → force stop" : "esc → confirm stop"
    }
    if (armed()) return "↑↓ select · enter open · esc leave select"
    if (focus() != null) return "↑↓ select · enter · esc → main"
    if (!canStop()) return "tab select · ↑↓ history"
    return forceStopWithoutConfirm()
      ? "tab select · ↑↓ history · esc → force stop"
      : "tab select · ↑↓ history · esc → confirm stop"
  }
  async function stopSwarm() {
    await requestStopSwarm()
  }

  /** One Esc handler — leave select → main → confirm/force stop. */
  function onEscape() {
    if (daemon.flagConfirm[0]()) return
    if (armed()) {
      daemon.setSwarmNavArmed(false)
      return
    }
    if (focus() != null) {
      daemon.setSwarmFocus(null)
      return
    }
    if (canStop()) {
      void requestStopSwarm()
    }
  }

  useBindings(() => ({
    priority: 1,
    bindings:
      dialogOpen() || !visible()
        ? []
        : [
            ...(showNav()
              ? [
                  {
                    key: "tab",
                    desc: "Toggle swarm select / chat",
                    group: "Artemis",
                    cmd: () => {
                      daemon.toggleSwarmNavArmed()
                      if (daemon.swarmNavArmed[0]() && focus() == null && cursor() === 0 && items().length > 1) {
                        daemon.setSwarmNavCursor(1)
                      }
                    },
                  },
                ]
              : []),
            ...(navActive()
              ? [
                  { key: "up", desc: "Select prev swarm page", group: "Artemis", cmd: () => move(-1) },
                  { key: "down", desc: "Select next swarm page", group: "Artemis", cmd: () => move(1) },
                  { key: "return", desc: "Open swarm page", group: "Artemis", cmd: () => openSelected() },
                ]
              : []),
            ...(escActive()
              ? [
                  {
                    key: "escape",
                    desc: armed()
                      ? "Leave swarm select"
                      : focus() != null
                        ? "Back to main"
                        : forceStopWithoutConfirm()
                          ? "Force stop swarm"
                          : "Confirm stop swarm",
                    group: "Artemis",
                    cmd: () => onEscape(),
                  },
                ]
              : []),
          ],
  }))

  return (
    <Show when={visible()}>
      <box flexShrink={0}>
        <box
          paddingTop={1}
          paddingBottom={1}
          paddingLeft={2}
          paddingRight={1}
          {...SplitBorder}
          border={["left"]}
          borderColor={navActive() ? theme.accent : theme.border}
          flexShrink={0}
          backgroundColor={theme.backgroundPanel}
          gap={1}
        >
          <box flexDirection="row" justifyContent="space-between" gap={1}>
            <box flexDirection="row" gap={1}>
              <Show
                when={spinStatus()}
                fallback={
                  <text fg={canStop() ? theme.warning : theme.textMuted} attributes={TextAttributes.BOLD}>
                    {statusLabel()}
                  </text>
                }
              >
                <Spinner color={theme.warning}>{statusLabel()}</Spinner>
              </Show>
              <Show when={elapsedLabel()}>
                <text fg={theme.textMuted}>· {elapsedLabel()}</text>
              </Show>
              <Show when={showNav()}>
                <text fg={theme.accent}>
                  {" "}
                  · page: {pageLabel()}
                </text>
                <Show when={armed()}>
                  <text fg={theme.warning}> · select</text>
                </Show>
              </Show>
            </box>
            <text fg={theme.textMuted}>{armHint()}</text>
          </box>
          <Show when={showNav()}>
            <For each={items()}>
              {(item, index) => {
                const selected = () => cursor() === index()
                const current = () => focus() === item.id
                const bg = () => {
                  if (hover() === item.label) return theme.backgroundElement
                  if (navActive() && selected()) return theme.backgroundElement
                  return theme.backgroundPanel
                }
                return (
                  <box
                    flexDirection="row"
                    gap={1}
                    backgroundColor={bg()}
                    onMouseOver={() => setHover(item.label)}
                    onMouseOut={() => setHover(null)}
                    onMouseUp={() => openAt(index())}
                  >
                    <text fg={navActive() && selected() ? theme.accent : theme.textMuted}>
                      {navActive() && selected() ? ">" : " "}
                    </text>
                    <text
                      fg={current() ? theme.primary : navActive() && selected() ? theme.text : theme.textMuted}
                      attributes={current() ? TextAttributes.BOLD : undefined}
                    >
                      {item.label}
                      {current() ? " · here" : ""}
                    </text>
                  </box>
                )
              }}
            </For>
          </Show>
        </box>
      </box>
    </Show>
  )
}
