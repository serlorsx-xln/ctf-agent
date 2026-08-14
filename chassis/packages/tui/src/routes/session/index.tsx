import {
  batch,
  createContext,
  createEffect,
  createMemo,
  createRenderEffect,
  createSignal,
  For,
  Match,
  on,
  onCleanup,
  onMount,
  Show,
  Switch,
  untrack,
  useContext,
} from "solid-js"
import { Dynamic } from "solid-js/web"
import path from "node:path"
import { mkdir, writeFile } from "node:fs/promises"
import { useRoute, useRouteData } from "../../context/route"
import { useProject } from "../../context/project"
import { useSync } from "../../context/sync"
import { useEvent } from "../../context/event"
import { SplitBorder } from "../../ui/border"
import { useTuiPaths, useTuiTerminalEnvironment } from "../../context/runtime"
import { Spinner } from "../../component/spinner"
import { createSyntaxStyleMemo, generateSubtleSyntax, selectedForeground, useTheme } from "../../context/theme"
import { BoxRenderable, ScrollBoxRenderable, addDefaultParsers, TextAttributes, RGBA } from "@opentui/core"
import { Prompt, type PromptRef } from "../../component/prompt"
import type {
  AssistantMessage,
  Part,
  Provider,
  ToolPart,
  UserMessage,
  TextPart,
  ReasoningPart,
  SessionStatus,
} from "@opencode-ai/sdk/v2"
import { useLocal } from "../../context/local"
import { Locale } from "../../util/locale"
import { formatDuration } from "../../util/format"
import { webSearchProviderLabel } from "../../util/tool-display"
import { hasAgentActivity, hasGlobalQuotaOutcome, hasTerminalSolveOutcome, dropPostSolveAgentChatter, type ArtemisEvent } from "../../util/artemis-live-log"
import {
  agentPreviews,
  globalSwarmEvents,
  listSwarmAgents,
  partitionEventsByAgent,
} from "../../util/artemis-swarm-agents"
import { DialogFlagsRequired } from "../../component/dialog-flags-required"
import { FlagConfirmBar } from "../../component/flag-confirm-bar"
import { SwarmAgentFooter } from "../../component/swarm-agent-footer"
import { DialogConfirmRestart, confirmStopWork } from "../../component/dialog-confirm-restart"
import { DialogConfirm } from "../../ui/dialog-confirm"
import {
  confirmRestartOpts,
  hasActiveSolveWork,
  hasDaemonArtemisResidue,
  solveGateOpen,
  stopAndClearArtemisState,
  swarmHasVisibleSolveUi,
} from "../../util/artemis-solve-state"
import { prefillModelFromRaceSpec } from "../../component/dialog-model"
import { daemon } from "../../artemis/client"
import { combinedPromptForLoad, looksLikeChallengePaste } from "../../util/artemis-challenge-paste"
import { tuiLoadChallenge } from "../../util/artemis-tui-load"
import { runSolveFlowGate } from "../../util/artemis-solve-flow"
import { useRenderer, useTerminalDimensions, type JSX } from "@opentui/solid"
import { useSDK } from "../../context/sdk"
import { useEditorContext } from "../../context/editor"
import { openEditor } from "../../editor"
import { useDialog } from "../../ui/dialog"
import { DialogAlert } from "../../ui/dialog-alert"
import { TodoItem } from "../../component/todo-item"
import { DialogMessage } from "./dialog-message"
import type { PromptInfo } from "../../component/prompt/history"
import { DialogTimeline } from "./dialog-timeline"
import { DialogForkFromTimeline } from "./dialog-fork-from-timeline"
import { DialogSessionRename } from "../../component/dialog-session-rename"
import { Sidebar } from "./sidebar"
import { SubagentFooter } from "./subagent-footer.tsx"
import { filetype } from "../../util/filetype"
import parsers from "../../parsers-config"
import { errorMessage } from "../../util/error"
import { Toast, useToast } from "../../ui/toast"
import { useKV } from "../../context/kv.tsx"
import stripAnsi from "strip-ansi"
import { usePromptRef } from "../../context/prompt"
import { useEpilogue } from "../../context/epilogue"
import { normalizePath } from "../../util/path"
import { PermissionPrompt } from "./permission"
import { QuestionPrompt } from "./question"
import { DialogExportOptions } from "../../ui/dialog-export-options"
import * as Model from "../../util/model"
import { formatTranscript } from "../../util/transcript"
import { sessionEpilogue } from "../../util/presentation"
import { setPreLayoutSiblingMargin } from "../../util/layout"
import { useTuiConfig } from "../../config"
import { useClipboard } from "../../context/clipboard"
import { nextThinkingMode, reasoningSummary, useThinkingMode, type ThinkingMode } from "../../context/thinking"
import { getScrollAcceleration } from "../../util/scroll"
import { collapseToolOutput } from "../../util/collapse-tool-output"
import { usePluginRuntime } from "../../plugin/runtime"
import { DialogRetryAction } from "../../component/dialog-retry-action"
import { getRevertDiffFiles } from "../../util/revert-diff"
import { OPENCODE_BASE_MODE, useBindings, useCommandShortcut, useOpencodeKeymap } from "../../keymap"
import { usePathFormatter } from "../../context/path-format"
import { LocationProvider } from "../../context/location"

addDefaultParsers(parsers.parsers)

const GO_UPSELL_FREE_TIER_LAST_SEEN_AT = "go_upsell_last_seen_at"
const GO_UPSELL_FREE_TIER_DONT_SHOW = "go_upsell_dont_show"
const GO_UPSELL_ACCOUNT_RATE_LIMIT_LAST_SEEN_AT = "go_upsell_account_rate_limit_last_seen_at"
const GO_UPSELL_ACCOUNT_RATE_LIMIT_DONT_SHOW = "go_upsell_account_rate_limit_dont_show"
const GO_UPSELL_WINDOW = 86_400_000 // 24 hrs
const GO_UPSELL_PROVIDERS = new Set<string>([])

export const alwaysSeparate = new WeakSet<BoxRenderable>()

type RetryAction = Extract<SessionStatus, { type: "retry" }>["action"]

function goUpsellKeys(action: RetryAction) {
  if (!action) return
  if (!GO_UPSELL_PROVIDERS.has(action.provider)) return
  if (action.reason === "free_tier_limit") {
    return {
      lastSeenAt: GO_UPSELL_FREE_TIER_LAST_SEEN_AT,
      dontShow: GO_UPSELL_FREE_TIER_DONT_SHOW,
    }
  }
  if (action.reason === "account_rate_limit") {
    return {
      lastSeenAt: GO_UPSELL_ACCOUNT_RATE_LIMIT_LAST_SEEN_AT,
      dontShow: GO_UPSELL_ACCOUNT_RATE_LIMIT_DONT_SHOW,
    }
  }
}

const sessionBindingCommands = [
  "session.share",
  "session.rename",
  "session.timeline",
  "session.fork",
  "session.compact",
  "session.unshare",
  "session.undo",
  "session.redo",
  "session.sidebar.toggle",
  "session.toggle.conceal",
  "session.toggle.timestamps",
  "session.toggle.thinking",
  "session.toggle.actions",
  "session.toggle.scrollbar",
  "session.toggle.generic_tool_output",
  "session.first",
  "session.last",
  "session.messages_last_user",
  "session.message.next",
  "session.message.previous",
  "messages.copy",
  "session.copy",
  "session.export",
  "session.child.first",
  "session.parent",
  "session.child.next",
  "session.child.previous",
] as const

const sessionGlobalBindingCommands = [
  "session.page.up",
  "session.page.down",
  "session.line.up",
  "session.line.down",
  "session.half.page.up",
  "session.half.page.down",
] as const

const sessionGlobalUnfocusedBindingCommands = ["session.first", "session.last"] as const

const context = createContext<{
  width: number
  sessionID: string
  conceal: () => boolean
  thinkingMode: () => ThinkingMode
  showThinking: () => boolean
  showTimestamps: () => boolean
  showDetails: () => boolean
  showGenericToolOutput: () => boolean
  diffWrapMode: () => "word" | "none"
  providers: () => ReadonlyMap<string, Provider>
  sync: ReturnType<typeof useSync>
  tui: ReturnType<typeof useTuiConfig>
}>()

function use() {
  const ctx = useContext(context)
  if (!ctx) throw new Error("useContext must be used within a Session component")
  return ctx
}

export function Session() {
  const setEpilogue = useEpilogue()
  const clipboard = useClipboard()
  const writeExport = async (file: string, content: string) => {
    await mkdir(path.dirname(file), { recursive: true })
    await writeFile(file, content)
  }
  const pluginRuntime = usePluginRuntime()
  const route = useRouteData("session")
  const { navigate } = useRoute()
  const sync = useSync()
  const event = useEvent()
  const project = useProject()
  const paths = useTuiPaths()
  const tuiConfig = useTuiConfig()
  const kv = useKV()
  const { theme } = useTheme()
  const promptRef = usePromptRef()
  const dialog = useDialog()
  // Bind the daemon peer before first paint. Do NOT read dialog.stack here —
  // that retriggers when the flags gate opens and immediately dismisses it
  // (load → empty "No solver activity").
  createRenderEffect(() => {
    if (process.env.ARTEMIS !== "1") return
    const sessionID = route.sessionID
    untrack(() => daemon.setSessionId(sessionID))
  })
  const session = createMemo(() => sync.session.get(route.sessionID))
  const location = createMemo(() => {
    const current = session()
    return current ? { directory: current.directory, workspaceID: current.workspaceID } : undefined
  })

  createEffect(() => {
    const title = Locale.truncate(session()?.title ?? "", 50)
    setEpilogue(sessionEpilogue({ title, sessionID: session()?.id }))
  })
  onCleanup(() => setEpilogue())
  const children = createMemo(() => {
    const parentID = session()?.parentID ?? session()?.id
    return sync.data.session
      .filter((x) => x.parentID === parentID || x.id === parentID)
      .toSorted((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  })
  const messages = createMemo(() => sync.data.message[route.sessionID] ?? [])
  const foregroundTasks = createMemo(() =>
    sync.data.capabilities.experimentalBackgroundSubagents
      ? messages().flatMap((message) =>
          (sync.data.part[message.id] ?? []).filter(
            (part): part is ToolPart =>
              part.type === "tool" &&
              part.tool === "task" &&
              part.state.status === "running" &&
              part.state.metadata?.background !== true,
          ),
        )
      : [],
  )
  const permissions = createMemo(() => {
    if (session()?.parentID) return []
    return children().flatMap((x) => sync.data.permission[x.id] ?? [])
  })
  const questions = createMemo(() => {
    if (session()?.parentID) return []
    return children().flatMap((x) => sync.data.question[x.id] ?? [])
  })
  const visible = createMemo(() => !session()?.parentID && permissions().length === 0 && questions().length === 0)
  /** Flags→mode→models dialogs still block the prompt (stack covers most cases). */
  const solveGateBusy = createMemo(
    () => process.env.ARTEMIS === "1" && daemon.solveFlowBusy[0](),
  )
  const disabled = createMemo(
    () =>
      permissions().length > 0 ||
      questions().length > 0 ||
      dialog.stack.length > 0 ||
      solveGateBusy() ||
      // Chat only on main — agent pages always lock the prompt.
      // Active Solving no longer locks main: send asks to stop work first.
      (process.env.ARTEMIS === "1" && daemon.swarmFocus[0]() != null),
  )

  const pending = createMemo(() => {
    const completed = messages().findLast((x) => x.role === "assistant" && x.time.completed)?.id
    return messages().findLast((x) => x.role === "assistant" && !x.time.completed && (!completed || x.id > completed))
      ?.id
  })

  const lastAssistant = createMemo(() => {
    return messages().findLast((x) => x.role === "assistant")
  })

  /** Intercept load (no LLM tool cards) still needs the solve feed on a fresh chat. */
  const hasSwarmToolCard = createMemo(() => {
    for (const msg of messages()) {
      const parts = sync.data.part[msg.id]
      if (!parts) continue
      for (const part of parts) {
        if (part.type !== "tool") continue
        if (part.tool === "artemis_ask_flags" || part.tool === "artemis_swarm") return true
      }
    }
    return false
  })
  const showSessionSwarm = createMemo(() => {
    if (process.env.ARTEMIS !== "1") return false
    if (hasSwarmToolCard()) return false
    // Gate dialogs own the screen — do not flash an empty grid over flags/mode.
    if (daemon.solveFlowBusy[0]()) return false
    return (
      daemon.swarmRunning[0]() ||
      daemon.swarmEvents[0]().length > 0 ||
      daemon.swarmStartedAt[0]() != null
    )
  })

  const dimensions = useTerminalDimensions()
  const [sidebar, setSidebar] = kv.signal<"auto" | "hide">("sidebar", "auto")
  const [sidebarOpen, setSidebarOpen] = createSignal(false)
  const [conceal, setConceal] = createSignal(true)
  const thinking = useThinkingMode()
  const thinkingMode = thinking.mode
  const showThinking = createMemo(() => true)
  const [timestamps, setTimestamps] = kv.signal<"hide" | "show">("timestamps", "hide")
  const [showDetails, setShowDetails] = kv.signal("tool_details_visibility", true)
  const [showAssistantMetadata, _setShowAssistantMetadata] = kv.signal("assistant_metadata_visibility", true)
  const [showScrollbar, setShowScrollbar] = kv.signal("scrollbar_visible", false)
  const [diffWrapMode] = kv.signal<"word" | "none">("diff_wrap_mode", "word")
  const [_animationsEnabled, _setAnimationsEnabled] = kv.signal("animations_enabled", true)
  const [showGenericToolOutput, setShowGenericToolOutput] = kv.signal("generic_tool_output_visibility", false)

  const wide = createMemo(() => dimensions().width > 120)
  const sidebarVisible = createMemo(() => {
    if (session()?.parentID) return false
    if (sidebarOpen()) return true
    if (sidebar() === "auto" && wide()) return true
    return false
  })
  const showTimestamps = createMemo(() => timestamps() === "show")
  const contentWidth = createMemo(() => dimensions().width - (sidebarVisible() ? 42 : 0) - 4)
  const providers = createMemo(() => Model.index(sync.data.provider))

  const scrollAcceleration = createMemo(() => getScrollAcceleration(tuiConfig))
  const toast = useToast()
  const sdk = useSDK()
  const editor = useEditorContext()

  createEffect(() => {
    const sessionID = route.sessionID
    void (async () => {
      const previousWorkspace = untrack(() => project.workspace.current())
      const result = await sdk.client.session.get({ sessionID }, { throwOnError: true })
      if (!result.data) {
        toast.show({
          message: `Session not found: ${sessionID}`,
          variant: "error",
          duration: 5000,
        })
        navigate({ type: "home" })
        return
      }

      if (result.data.workspaceID !== previousWorkspace) {
        project.workspace.set(result.data.workspaceID)

        // Sync all the data for this workspace. Note that this
        // workspace may not exist anymore which is why this is not
        // fatal. If it doesn't we still want to show the session
        // (which will be non-interactive)
        try {
          await sync.bootstrap({ fatal: false })
        } catch {}
      }
      editor.reconnect(result.data.directory)
      await sync.session.sync(sessionID)
      if (route.sessionID === sessionID && scroll) scroll.scrollBy(100_000)
    })().catch((error) => {
      if (route.sessionID !== sessionID) return
      toast.show({
        message: errorMessage(error),
        variant: "error",
        duration: 5000,
      })
      navigate({ type: "home" })
    })
  })

  let lastSwitch: string | undefined = undefined
  event.on("message.part.updated", (evt) => {
    const part = evt.properties.part
    if (part.type !== "tool") return
    if (part.sessionID !== route.sessionID) return
    if (part.state.status !== "completed") return
    if (part.id === lastSwitch) return

    if (part.tool === "plan_exit") {
      local.agent.set("build")
      lastSwitch = part.id
    } else if (part.tool === "plan_enter") {
      local.agent.set("plan")
      lastSwitch = part.id
    }
  })

  let seeded = false
  let scroll: ScrollBoxRenderable
  let prompt: PromptRef | undefined
  const bind = (r: PromptRef | undefined) => {
    prompt = r
    promptRef.set(r)
    if (seeded || !route.prompt || !r) return
    seeded = true
    r.set(route.prompt)
  }
  const keymap = useOpencodeKeymap()
  const local = useLocal()
  const renderer = useRenderer()

  // --- Flag-count + flag-confirm dialogs (Session-level, not ArtemisSwarm) ---
  // These must live here (not in ArtemisSwarm) because the tool part completes
  // immediately after ask_flags returns, unmounting ArtemisSwarm and disposing
  // any effect inside it. We use direct callbacks (not signals) because SolidJS
  // signals created outside the TUI reactive root don't reliably notify effects
  // inside it.
  const answered = new Set<string>()
  const seenAskFlagsParts = new Set<string>()
  let gateGeneration = 0

  async function clearSolvedSession(opts?: { newChat?: boolean }): Promise<boolean> {
    await stopAndClearArtemisState()
    seenAskFlagsParts.clear()
    answered.clear()

    if (opts?.newChat === false) return true

    // New OpenCode session so old Load/Solve cards do not stack in the transcript.
    try {
      const agent = local.agent.current()
      const model = local.model.current()
      const res = await sdk.client.session.create({
        agent: agent?.name,
        model: model
          ? {
              providerID: model.providerID,
              id: model.modelID,
              variant: local.model.variant.current(),
            }
          : undefined,
      })
      if (res.error || !res.data?.id) return false
      navigate({ type: "session", sessionID: res.data.id })
      return true
    } catch {
      return false
    }
  }

  /** Daemon residue plus Artemis tool cards left in this transcript. */
  function hasPriorArtemisState(): boolean {
    if (hasDaemonArtemisResidue()) return true
    // Failed loads still leave Artemis tool cards in the chat transcript.
    for (const msg of messages()) {
      const parts = sync.data.part[msg.id]
      if (!parts) continue
      for (const part of parts) {
        if (part.type !== "tool") continue
        if (
          part.tool === "artemis_ask_flags" ||
          part.tool === "artemis_swarm" ||
          part.tool === "artemis_load_challenge" ||
          part.tool === "artemis_status"
        ) {
          return true
        }
      }
    }
    return false
  }

  /**
   * Load challenge + open solve gate from the prompt text, without sending to
   * the chat LLM. Works for any chat provider (Cursor, Claude, GPT, Gemini).
   * Returns true when the submit was handled (caller must return false).
   */
  function promptTextForLoad(): string {
    return combinedPromptForLoad(prompt?.current.input ?? "", prompt?.current.parts ?? [])
  }

  async function tryLoadChallengeFromPrompt(): Promise<boolean> {
    const raw = promptTextForLoad()
    if (!looksLikeChallengePaste(raw)) return false
    if (daemon.swarmRunning[0]() || daemon.solveLocked[0]() || daemon.solveFlowBusy[0]()) {
      return false
    }
    toast.show({ message: "Loading challenge…", variant: "info" })
    const result = await tuiLoadChallenge(raw)
    if (result.status === "skip") return false
    if (result.status === "error") {
      toast.show({ message: result.message, variant: "error" })
      return true
    }
    prompt?.reset()
    daemon.setSuppressSolveGate(false)
    startSolveGate({
      challengeName: result.session_state.challenge_name,
      defaultFlags: Number(result.session_state.flags_required ?? 1) || 1,
    })
    return true
  }

  async function beforePromptSubmit(): Promise<boolean> {
    if (process.env.ARTEMIS !== "1") return true
    if (dialog.stack.length > 0 || daemon.solveFlowBusy[0]()) return false
    if (daemon.swarmFocus[0]() != null) {
      toast.show({ message: "Esc → main to chat", variant: "warning" })
      return false
    }
    const raw = promptTextForLoad()
    if (looksLikeChallengePaste(raw)) {
      if (hasPriorArtemisState()) {
        const ok = await DialogConfirmRestart.show(dialog, confirmRestartOpts())
        if (!ok) return false
        await clearSolvedSession({ newChat: false })
        if (route.sessionID) {
          await sdk.client.session.abort({ sessionID: route.sessionID }).catch(() => {})
        }
      }
      daemon.setSuppressSolveGate(false)
      await tryLoadChallengeFromPrompt()
      return false
    }
    if (!hasPriorArtemisState()) {
      toast.show({
        message: "Paste a challenge, path, or @files. Artemis loads first, then asks flags / mode / models.",
        variant: "info",
      })
      return false
    }
    if (daemon.solveLocked[0]() || daemon.swarmRunning[0]()) {
      abortCoordinator()
      return false
    }
    // Loaded but not finished — keep the chat model off until Start or a new paste.
    if (daemon.sessionState[0]().challenge_dir && !daemon.flowCompleted[0]()) {
      abortCoordinator()
      return false
    }
    return true
  }

  function abortCoordinator() {
    if (route.sessionID) {
      void sdk.client.session.abort({ sessionID: route.sessionID }).catch(() => {})
    }
  }

  function startSolveGate(opts?: {
    challengeName?: string
    preselected?: string[]
    defaultFlags?: number
  }): boolean {
    if (daemon.solveFlowBusy[0]()) return false
    if (daemon.swarmRunning[0]()) return false
    if (daemon.solveLocked[0]()) return false
    // Quota / CORRECT / stop: do not auto-reopen flags→mode→models.
    if (daemon.suppressSolveGate[0]()) return false
    abortCoordinator()
    daemon.setSolveFlowBusy(true)
    // Drop previous swarm UI so the gate is not drawn over stale agents / esc-stop.
    // Keep lastModels until the operator confirms a new pick (cancel must not
    // leave an empty "No solver activity" grid).
    daemon.clearSwarmEvents()
    daemon.setSolveLocked(true)
    const gen = ++gateGeneration
    const sessionAtStart = route.sessionID
    void runSolveFlowGate(dialog, {
      challengeName: opts?.challengeName ?? daemon.sessionState[0]().challenge_name,
      preselected: opts?.preselected,
      defaultFlags: opts?.defaultFlags ?? 1,
    })
      .then(async (result) => {
        if (gen !== gateGeneration || route.sessionID !== sessionAtStart) return
        if (!result) {
          daemon.setSolveLocked(false)
          // Cancelled gate — do not let the same ask_flags card reopen immediately.
          daemon.setSuppressSolveGate(true)
          daemon.setSolveFlowBusy(false)
          return
        }
        // Clear again right before start — covers reconnect replay during dialogs.
        daemon.clearSwarmEvents()
        daemon.setLastModels(result.models)
        daemon.setLastFlagsRequired(result.flags)
        // Prefer a Cursor race model for orchestrator prefill; never switch Artemis
        // chat onto ChatGPT/Codex/Claude (breaks stub + causes quota red banners).
        const cursorSpec = result.models.find((m) => m.trim().startsWith("cursor/"))
        if (cursorSpec) prefillModelFromRaceSpec(cursorSpec, local)
        // Drop busy BEFORE startSwarm so daemon boot/roster pushes are not ignored
        // while the RPC is in flight (spawn broadcasts before the response).
        daemon.setSolveFlowBusy(false)
        try {
          await daemon.startSwarm({
            models: result.models,
            flags_required: result.flags,
            force: true,
            auto_confirm: false,
          })
          abortCoordinator()
        } catch {
          // startSwarm reconciles with daemon — do not clear running blindly
          // (RPC timeout can race a successful spawn).
          // Keep lastModels + roster from startSwarm so the 3-agent grid stays visible.
        }
      })
      .finally(() => {
        if (gen !== gateGeneration || route.sessionID !== sessionAtStart) return
        daemon.setSolveFlowBusy(false)
      })
    return true
  }

  onMount(() => {
    // Keyed remount leaves DialogProvider's stack intact. Clear leftovers once
    // — never reactively, or the flags/mode/models gate closes itself.
    if (
      process.env.ARTEMIS === "1" &&
      !daemon.swarmRunning[0]() &&
      !daemon.solveFlowBusy[0]() &&
      dialog.stack.length > 0
    ) {
      dialog.clear()
    }
    // Must subscribe before ask_flags — plugin runs in another process and only
    // broadcasts solve_flow_request to connected TUI sockets.
    // Clear leftovers only when idle with no finished summary — remount must not
    // wipe a live run or erase the quota/CORRECT grid the operator is reading.
    if (!daemon.swarmRunning[0]() && !daemon.solveLocked[0]()) {
      const multi = daemon.swarmRoster[0]().length > 1 || daemon.lastModels[0]().length > 1
      if (!hasTerminalSolveOutcome(daemon.swarmEvents[0](), multi)) {
        daemon.clearSwarmEvents()
      }
    }
    daemon.setSessionId(route.sessionID)
    void daemon.ensureConnected()

    const pendingGate = daemon.takePendingSolveGate()
    if (pendingGate) {
      startSolveGate({
        challengeName: pendingGate.challenge ?? daemon.sessionState[0]().challenge_name,
        preselected: pendingGate.preselected,
        defaultFlags: pendingGate.default_flags,
      })
    }

    const offSolve = daemon.onSolveFlow((push) => {
      // New load RPC — not a remount of a cancelled card.
      if (push.from_load) daemon.setSuppressSolveGate(false)
      startSolveGate({
        challengeName: push.challenge ?? daemon.sessionState[0]().challenge_name,
        preselected: push.preselected,
        defaultFlags: push.default_flags,
      })
    })

    const tryOpenGateFromToolPart = (
      part: ToolPart,
      sessionID: string,
      opts?: { fresh?: boolean },
    ) => {
      if (part.sessionID && part.sessionID !== sessionID) return
      const isLoad = part.tool === "artemis_load_challenge"
      const isAsk = part.tool === "artemis_ask_flags" || part.tool === "artemis_swarm"
      if (!isLoad && !isAsk) return
      if (seenAskFlagsParts.has(part.id)) return
      if (isLoad) {
        if (part.state.status !== "completed") return
        const out = typeof part.state.output === "string" ? part.state.output : ""
        if (/^ERROR/i.test(out.trim())) return
        // Fresh part.updated only — remount scan must not undo Esc.
        if (opts?.fresh) daemon.setSuppressSolveGate(false)
      } else if (part.state.status !== "running" && part.state.status !== "completed") {
        return
      }
      const started = startSolveGate({ defaultFlags: 1 })
      if (
        started ||
        daemon.solveFlowBusy[0]() ||
        daemon.solveLocked[0]() ||
        daemon.swarmRunning[0]() ||
        daemon.suppressSolveGate[0]()
      ) {
        seenAskFlagsParts.add(part.id)
      }
    }

    // Fallback: if the daemon push arrived before we subscribed, still open the
    // gate when load/ask_flags/swarm tool cards appear. Mark the part whenever
    // the gate is already owned (busy/locked/running/suppressed) so unlocking
    // after quota cannot reopen the same card.
    const offTool = event.on("message.part.updated", (evt) => {
      if (process.env.ARTEMIS !== "1") return
      const part = evt.properties.part
      if (part.type !== "tool") return
      tryOpenGateFromToolPart(part, route.sessionID, { fresh: true })
    })

    // Relaunch / remount: the load card is already completed, so no new
    // part.updated fires. Scan once so flags → models still opens.
    for (const msg of messages()) {
      const parts = sync.data.part[msg.id]
      if (!parts) continue
      for (const part of parts) {
        if (part.type !== "tool") continue
        tryOpenGateFromToolPart(part, route.sessionID)
      }
    }

    const offAsk = daemon.onFlagsAsk((push) => {
      // Solve-flow gate owns flags; ignore legacy FLAGS_ASK during/after a run.
      if (daemon.solveFlowBusy[0]() || daemon.solveLocked[0]() || daemon.suppressSolveGate[0]()) return
      if (daemon.sessionState[0]().flags_explicit) return
      if (answered.has(`ask:${push.request_id}`)) return
      answered.add(`ask:${push.request_id}`)
      void DialogFlagsRequired.show(dialog, {
        defaultValue: push.default,
        challengeName: push.challenge,
        cancelable: false,
      }).then((n) => {
        const value = n ?? Math.max(1, Math.min(64, push.default ?? 1))
        void daemon.answerFlagsAsk(push.request_id, value).catch(() => {})
      })
    })
    // Flag confirm: inline FlagConfirmBar reads daemon.flagConfirm (no modal).
    onCleanup(() => {
      gateGeneration += 1
      offSolve()
      offTool()
      offAsk()
    })
  })

  event.on("session.status", (evt) => {
    if (evt.properties.sessionID !== route.sessionID) return
    if (evt.properties.status.type !== "retry") return
    if (!evt.properties.status.action) return
    if (dialog.stack.length > 0) return

    const keys = goUpsellKeys(evt.properties.status.action)
    if (!keys) return

    const seen = kv.get(keys.lastSeenAt)
    if (typeof seen === "number" && Date.now() - seen < GO_UPSELL_WINDOW) return

    if (kv.get(keys.dontShow)) return

    void DialogRetryAction.show(dialog, evt.properties.status.action).then((dontShowAgain) => {
      if (dontShowAgain) kv.set(keys.dontShow, true)
      kv.set(keys.lastSeenAt, Date.now())
    })
  })

  // Helper: Find next visible message boundary in direction
  const findNextVisibleMessage = (direction: "next" | "prev"): string | null => {
    const children = scroll.getChildren()
    const messagesList = messages()
    const scrollTop = scroll.y

    // Get visible messages sorted by position, filtering for valid non-synthetic, non-ignored content
    const visibleMessages = children
      .filter((c) => {
        if (!c.id) return false
        const message = messagesList.find((m) => m.id === c.id)
        if (!message) return false

        // Check if message has valid non-synthetic, non-ignored text parts
        const parts = sync.data.part[message.id]
        if (!parts || !Array.isArray(parts)) return false

        return parts.some((part) => part && part.type === "text" && !part.synthetic && !part.ignored)
      })
      .sort((a, b) => a.y - b.y)

    if (visibleMessages.length === 0) return null

    if (direction === "next") {
      // Find first message below current position
      return visibleMessages.find((c) => c.y > scrollTop + 10)?.id ?? null
    }
    // Find last message above current position
    return [...visibleMessages].reverse().find((c) => c.y < scrollTop - 10)?.id ?? null
  }

  // Helper: Scroll to message in direction or fallback to page scroll
  const scrollToMessage = (direction: "next" | "prev", dialog: ReturnType<typeof useDialog>) => {
    const targetID = findNextVisibleMessage(direction)

    if (!targetID) {
      scroll.scrollBy(direction === "next" ? scroll.height : -scroll.height)
      dialog.clear()
      return
    }

    const child = scroll.getChildren().find((c) => c.id === targetID)
    if (child) scroll.scrollBy(child.y - scroll.y - 1)
    dialog.clear()
  }

  function toBottom() {
    setTimeout(() => {
      if (!scroll || scroll.isDestroyed) return
      scroll.scrollTo(scroll.scrollHeight)
    }, 50)
  }

  function enterChild(sessionID: string) {
    navigate({
      type: "session",
      sessionID,
    })
    const status = sync.data.session_status[sessionID]
    if (status?.type === "retry") void DialogAlert.show(dialog, "Retry Error", status.message)
  }

  function moveFirstChild() {
    if (children().length === 1) return
    const next = children().find((x) => !!x.parentID)
    if (next) enterChild(next.id)
  }

  function moveChild(direction: number) {
    if (children().length === 1) return

    const sessions = children().filter((x) => !!x.parentID)
    let next = sessions.findIndex((x) => x.id === session()?.id) - direction

    if (next >= sessions.length) next = 0
    if (next < 0) next = sessions.length - 1
    if (sessions[next]) enterChild(sessions[next].id)
  }

  function childSessionHandler(func: () => void) {
    return () => {
      if (!session()?.parentID || dialog.stack.length > 0) return
      func()
    }
  }

  const sessionCommandList = createMemo(() => [
    ...(process.env.ARTEMIS === "1"
      ? [
          {
            title: "Restart challenge",
            value: "artemis.restart",
            category: "Session",
            slash: {
              name: "restart",
              aliases: ["reset"],
            },
            run: async () => {
              if (solveGateOpen()) {
                toast.show({ message: "Finish or cancel the current dialog first", variant: "warning" })
                return
              }
              const opts = confirmRestartOpts()
              const ok = await DialogConfirmRestart.show(dialog, opts)
              if (!ok) return
              const cleared = await clearSolvedSession({ newChat: true })
              toast.show({
                message: cleared
                  ? opts.active
                    ? "Stopped and cleared — paste a new path"
                    : "Challenge cleared — paste a new path"
                  : "Challenge data cleared",
                variant: "success",
              })
              dialog.clear()
              return
            },
          },
          {
            title: "Stop swarm",
            value: "artemis.stop",
            category: "Session",
            slash: {
              name: "stop",
              aliases: ["swarm-stop"],
            },
            run: async () => {
              if (solveGateOpen()) {
                toast.show({ message: "Close the dialog first (esc cancel)", variant: "warning" })
                return
              }
              if (!hasActiveSolveWork()) {
                toast.show({ message: "No swarm running", variant: "warning" })
                dialog.clear()
                return
              }
              const multi =
                daemon.swarmRoster[0]().length > 1 || daemon.lastModels[0]().length > 1
              const alreadyDone = hasTerminalSolveOutcome(daemon.swarmEvents[0](), multi)
              if (!alreadyDone) {
                const ok = await confirmStopWork(dialog)
                if (!ok) return
              }
              try {
                // Keep unlock timer armed until swarm_exit — clearing on RPC ack
                // defeats hung-cleanup recovery (footer Esc path leaves it armed).
                const stopGen = daemon.bumpSwarmStopGeneration()
                setTimeout(() => {
                  if (stopGen !== daemon.swarmStopGeneration()) return
                  if (daemon.swarmRunning[0]()) {
                    daemon.swarmRunning[1](false)
                    daemon.setSolveLocked(false)
                    if (daemon.swarmEndedAt[0]() == null) daemon.swarmEndedAt[1](Date.now())
                    toast.show({
                      message: "Stopped locally — cleanup still running in background",
                      variant: "warning",
                    })
                  }
                }, 8000)
                await daemon.request("swarm_stop", {})
                daemon.flagConfirm[1](null)
                toast.show({
                  message: alreadyDone ? "Force-stopping…" : "Stopping swarm…",
                  variant: "info",
                })
              } catch (e) {
                daemon.swarmRunning[1](false)
                daemon.setSolveLocked(false)
                toast.show({
                  message: `Stop failed: ${e instanceof Error ? e.message : String(e)}`,
                  variant: "error",
                })
              }
              dialog.clear()
            },
          },
        ]
      : []),
    {
      title: session()?.share?.url ? "Copy share link" : "Share session",
      value: "session.share",
      suggested: route.type === "session",
      category: "Session",
      enabled: sync.data.config.share !== "disabled",
      slash: {
        name: "share",
      },
      run: async () => {
        const copy = (url: string) =>
          clipboard
            .write?.(url)
            .then(() => toast.show({ message: "Share URL copied to clipboard!", variant: "success" }))
            .catch(() => toast.show({ message: "Failed to copy URL to clipboard", variant: "error" }))
        const url = session()?.share?.url
        if (url) {
          await copy(url)
          dialog.clear()
          return
        }
        if (!kv.get("share_consent", false)) {
          const ok = await DialogConfirm.show(dialog, "Share Session", "Are you sure you want to share it?")
          if (ok !== true) return
          kv.set("share_consent", true)
        }
        await sdk.client.session
          .share({
            sessionID: route.sessionID,
          })
          .then((res) => copy(res.data!.share!.url))
          .catch((error) => {
            toast.show({
              message: error instanceof Error ? error.message : "Failed to share session",
              variant: "error",
            })
          })
        dialog.clear()
      },
    },
    {
      title: "Rename session",
      value: "session.rename",
      category: "Session",
      slash: {
        name: "rename",
      },
      run: () => {
        dialog.replace(() => <DialogSessionRename session={route.sessionID} />)
      },
    },
    {
      title: "Jump to message",
      value: "session.timeline",
      category: "Session",
      slash: {
        name: "timeline",
      },
      run: () => {
        dialog.replace(() => (
          <DialogTimeline
            onMove={(messageID) => {
              const child = scroll.getChildren().find((child) => {
                return child.id === messageID
              })
              if (child) scroll.scrollBy(child.y - scroll.y - 1)
            }}
            sessionID={route.sessionID}
            setPrompt={(promptInfo) => prompt?.set(promptInfo)}
          />
        ))
      },
    },
    {
      title: "Fork session",
      value: "session.fork",
      category: "Session",
      slash: {
        name: "fork",
      },
      run: () => {
        dialog.replace(() => (
          <DialogForkFromTimeline
            onMove={(messageID) => {
              if (!messageID) return
              const child = scroll.getChildren().find((child) => {
                return child.id === messageID
              })
              if (child) scroll.scrollBy(child.y - scroll.y - 1)
            }}
            sessionID={route.sessionID}
          />
        ))
      },
    },
    {
      title: "Compact session",
      value: "session.compact",
      category: "Session",
      slash: {
        name: "compact",
        aliases: ["summarize"],
      },
      run: () => {
        const selectedModel = local.model.current()
        if (!selectedModel) {
          toast.show({
            variant: "warning",
            message: "Connect a provider to summarize this session",
            duration: 3000,
          })
          return
        }
        void sdk.client.session.summarize({
          sessionID: route.sessionID,
          modelID: selectedModel.modelID,
          providerID: selectedModel.providerID,
        })
        dialog.clear()
      },
    },
    {
      title: "Unshare session",
      value: "session.unshare",
      category: "Session",
      enabled: !!session()?.share?.url,
      slash: {
        name: "unshare",
      },
      run: async () => {
        await sdk.client.session
          .unshare({
            sessionID: route.sessionID,
          })
          .then(() => toast.show({ message: "Session unshared successfully", variant: "success" }))
          .catch((error) => {
            toast.show({
              message: error instanceof Error ? error.message : "Failed to unshare session",
              variant: "error",
            })
          })
        dialog.clear()
      },
    },
    {
      title: "Undo previous message",
      value: "session.undo",
      category: "Session",
      slash: {
        name: "undo",
      },
      run: async () => {
        const status = sync.data.session_status?.[route.sessionID]
        if (status?.type !== "idle") await sdk.client.session.abort({ sessionID: route.sessionID }).catch(() => {})
        const revert = session()?.revert?.messageID
        const message = messages().findLast((x) => (!revert || x.id < revert) && x.role === "user")
        if (!message) return
        void sdk.client.session
          .revert({
            sessionID: route.sessionID,
            messageID: message.id,
          })
          .then(() => {
            toBottom()
          })
        const parts = sync.data.part[message.id]
        prompt?.set(
          parts.reduce(
            (agg, part) => {
              if (part.type === "text") {
                if (!part.synthetic) agg.input += part.text
              }
              if (part.type === "file") agg.parts.push(part)
              return agg
            },
            { input: "", parts: [] as PromptInfo["parts"] },
          ),
        )
        dialog.clear()
      },
    },
    {
      title: "Redo",
      value: "session.redo",
      category: "Session",
      enabled: !!session()?.revert?.messageID,
      slash: {
        name: "redo",
      },
      run: () => {
        dialog.clear()
        const messageID = session()?.revert?.messageID
        if (!messageID) return
        const message = messages().find((x) => x.role === "user" && x.id > messageID)
        if (!message) {
          void sdk.client.session.unrevert({
            sessionID: route.sessionID,
          })
          prompt?.set({ input: "", parts: [] })
          return
        }
        void sdk.client.session.revert({
          sessionID: route.sessionID,
          messageID: message.id,
        })
      },
    },
    {
      title: sidebarVisible() ? "Hide sidebar" : "Show sidebar",
      value: "session.sidebar.toggle",
      category: "Session",
      run: () => {
        batch(() => {
          const isVisible = sidebarVisible()
          setSidebar(() => (isVisible ? "hide" : "auto"))
          setSidebarOpen(!isVisible)
        })
        dialog.clear()
      },
    },
    {
      title: conceal() ? "Disable code concealment" : "Enable code concealment",
      value: "session.toggle.conceal",
      category: "Session",
      run: () => {
        setConceal((prev) => !prev)
        dialog.clear()
      },
    },
    {
      title: showTimestamps() ? "Hide timestamps" : "Show timestamps",
      value: "session.toggle.timestamps",
      category: "Session",
      slash: {
        name: "timestamps",
        aliases: ["toggle-timestamps"],
      },
      run: () => {
        setTimestamps((prev) => (prev === "show" ? "hide" : "show"))
        dialog.clear()
      },
    },
    {
      title: (() => {
        const next = nextThinkingMode(thinkingMode())
        if (next === "hide") return "Collapse thinking"
        return "Expand thinking"
      })(),
      value: "session.toggle.thinking",
      category: "Session",
      slash: {
        name: "thinking",
        aliases: ["toggle-thinking"],
      },
      run: () => {
        thinking.set(nextThinkingMode(thinkingMode()))
        dialog.clear()
      },
    },
    {
      title: showDetails() ? "Hide tool details" : "Show tool details",
      value: "session.toggle.actions",
      category: "Session",
      run: () => {
        setShowDetails((prev) => !prev)
        dialog.clear()
      },
    },
    {
      title: "Toggle session scrollbar",
      value: "session.toggle.scrollbar",
      category: "Session",
      run: () => {
        setShowScrollbar((prev) => !prev)
        dialog.clear()
      },
    },
    {
      title: showGenericToolOutput() ? "Hide generic tool output" : "Show generic tool output",
      value: "session.toggle.generic_tool_output",
      category: "Session",
      run: () => {
        setShowGenericToolOutput((prev) => !prev)
        dialog.clear()
      },
    },
    {
      title: "Page up",
      value: "session.page.up",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollBy(-scroll.height / 2)
        dialog.clear()
      },
    },
    {
      title: "Page down",
      value: "session.page.down",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollBy(scroll.height / 2)
        dialog.clear()
      },
    },
    {
      title: "Line up",
      value: "session.line.up",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollBy(-1)
        dialog.clear()
      },
    },
    {
      title: "Line down",
      value: "session.line.down",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollBy(1)
        dialog.clear()
      },
    },
    {
      title: "Half page up",
      value: "session.half.page.up",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollBy(-scroll.height / 4)
        dialog.clear()
      },
    },
    {
      title: "Half page down",
      value: "session.half.page.down",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollBy(scroll.height / 4)
        dialog.clear()
      },
    },
    {
      title: "First message",
      value: "session.first",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollTo(0)
        dialog.clear()
      },
    },
    {
      title: "Last message",
      value: "session.last",
      category: "Session",
      hidden: true,
      run: () => {
        scroll.scrollTo(scroll.scrollHeight)
        dialog.clear()
      },
    },
    {
      title: "Jump to last user message",
      value: "session.messages_last_user",
      category: "Session",
      hidden: true,
      run: () => {
        const messages = sync.data.message[route.sessionID]
        if (!messages || !messages.length) return

        // Find the most recent user message with non-ignored, non-synthetic text parts
        for (let i = messages.length - 1; i >= 0; i--) {
          const message = messages[i]
          if (!message || message.role !== "user") continue

          const parts = sync.data.part[message.id]
          if (!parts || !Array.isArray(parts)) continue

          const hasValidTextPart = parts.some(
            (part) => part && part.type === "text" && !part.synthetic && !part.ignored,
          )

          if (hasValidTextPart) {
            const child = scroll.getChildren().find((child) => {
              return child.id === message.id
            })
            if (child) scroll.scrollBy(child.y - scroll.y - 1)
            break
          }
        }
      },
    },
    {
      title: "Next message",
      value: "session.message.next",
      category: "Session",
      hidden: true,
      run: () => scrollToMessage("next", dialog),
    },
    {
      title: "Previous message",
      value: "session.message.previous",
      category: "Session",
      hidden: true,
      run: () => scrollToMessage("prev", dialog),
    },
    {
      title: "Copy last assistant message",
      value: "messages.copy",
      category: "Session",
      run: () => {
        const revertID = session()?.revert?.messageID
        const lastAssistantMessage = messages().findLast(
          (msg) => msg.role === "assistant" && (!revertID || msg.id < revertID),
        )
        if (!lastAssistantMessage) {
          toast.show({ message: "No assistant messages found", variant: "error" })
          dialog.clear()
          return
        }

        const parts = sync.data.part[lastAssistantMessage.id] ?? []
        const textParts = parts.filter((part) => part.type === "text")
        if (textParts.length === 0) {
          toast.show({ message: "No text parts found in last assistant message", variant: "error" })
          dialog.clear()
          return
        }

        const text = textParts
          .map((part) => part.text)
          .join("\n")
          .trim()
        if (!text) {
          toast.show({
            message: "No text content found in last assistant message",
            variant: "error",
          })
          dialog.clear()
          return
        }

        clipboard
          .write?.(text)
          .then(() => toast.show({ message: "Message copied to clipboard!", variant: "success" }))
          .catch(() => toast.show({ message: "Failed to copy to clipboard", variant: "error" }))
        dialog.clear()
      },
    },
    {
      title: "Copy session transcript",
      value: "session.copy",
      category: "Session",
      slash: {
        name: "copy",
      },
      run: async () => {
        try {
          const sessionData = session()
          if (!sessionData) return
          const sessionMessages = messages()
          const transcript = formatTranscript(
            sessionData,
            sessionMessages.map((msg) => ({ info: msg, parts: sync.data.part[msg.id] ?? [] })),
            {
              thinking: showThinking(),
              toolDetails: showDetails(),
              assistantMetadata: showAssistantMetadata(),
              providers: sync.data.provider,
            },
          )
          await clipboard.write?.(transcript)
          toast.show({ message: "Session transcript copied to clipboard!", variant: "success" })
        } catch {
          toast.show({ message: "Failed to copy session transcript", variant: "error" })
        }
        dialog.clear()
      },
    },
    {
      title: "Export session transcript",
      value: "session.export",
      category: "Session",
      slash: {
        name: "export",
      },
      run: async () => {
        try {
          const sessionData = session()
          if (!sessionData) return
          const sessionMessages = messages()

          const defaultFilename = `session-${sessionData.id.slice(0, 8)}.md`

          const options = await DialogExportOptions.show(
            dialog,
            defaultFilename,
            showThinking(),
            showDetails(),
            showAssistantMetadata(),
            false,
          )

          if (options === null) return

          const transcript = formatTranscript(
            sessionData,
            sessionMessages.map((msg) => ({ info: msg, parts: sync.data.part[msg.id] ?? [] })),
            {
              thinking: options.thinking,
              toolDetails: options.toolDetails,
              assistantMetadata: options.assistantMetadata,
              providers: sync.data.provider,
            },
          )

          if (options.openWithoutSaving) {
            // Just open in editor without saving
            await openEditor({
              renderer,
              value: transcript,
              cwd:
                (project.instance.path().worktree === "/" ? undefined : project.instance.path().worktree) ||
                project.instance.directory() ||
                paths.cwd,
            })
          } else {
            const exportDir = paths.cwd
            const filename = options.filename.trim()
            const filepath = path.join(exportDir, filename)

            await writeExport(filepath, transcript)

            // Open with EDITOR if available
            const result = await openEditor({
              renderer,
              value: transcript,
              cwd:
                (project.instance.path().worktree === "/" ? undefined : project.instance.path().worktree) ||
                project.instance.directory() ||
                paths.cwd,
            })
            if (result !== undefined) {
              await writeExport(filepath, result)
            }

            toast.show({ message: `Session exported to ${filename}`, variant: "success" })
          }
        } catch {
          toast.show({ message: "Failed to export session", variant: "error" })
        }
        dialog.clear()
      },
    },
    {
      title: "Background subagents",
      value: "session.background",
      category: "Session",
      hidden: true,
      enabled: foregroundTasks().length > 0,
      run: () => {
        void sdk.client.experimental.session.background({
          sessionID: route.sessionID,
          workspace: project.workspace.current(),
        })
        dialog.clear()
      },
    },
    {
      title: "Go to child session",
      value: "session.child.first",
      category: "Session",
      hidden: true,
      run: () => {
        dialog.clear()
        moveFirstChild()
      },
    },
    {
      title: "Go to parent session",
      value: "session.parent",
      category: "Session",
      hidden: true,
      enabled: !!session()?.parentID,
      run: childSessionHandler(() => {
        const parentID = session()?.parentID
        if (parentID) {
          navigate({
            type: "session",
            sessionID: parentID,
          })
        }
        dialog.clear()
      }),
    },
    {
      title: "Next child session",
      value: "session.child.next",
      category: "Session",
      hidden: true,
      enabled: !!session()?.parentID,
      run: childSessionHandler(() => {
        dialog.clear()
        moveChild(1)
      }),
    },
    {
      title: "Previous child session",
      value: "session.child.previous",
      category: "Session",
      hidden: true,
      enabled: !!session()?.parentID,
      run: childSessionHandler(() => {
        dialog.clear()
        moveChild(-1)
      }),
    },
  ])

  const sessionCommands = createMemo(() =>
    sessionCommandList()
      .filter((command) => {
        if (process.env.ARTEMIS !== "1") return true
        // Keep challenge slash commands + hidden keybind helpers.
        if (command.value === "artemis.restart" || command.value === "artemis.stop") return true
        if (command.hidden === true) return true
        if (!("slash" in command) || !command.slash?.name) return true
        return false
      })
      .map((command) => ({
        namespace: "palette",
        name: command.value,
        desc: "description" in command ? command.description : undefined,
        slashName: "slash" in command ? command.slash?.name : undefined,
        slashAliases: "slash" in command ? command.slash?.aliases : undefined,
        ...command,
      })),
  )

  useBindings(() => ({
    commands: sessionCommands(),
  }))

  useBindings(() => ({
    bindings: tuiConfig.keybinds.gather("session.global", sessionGlobalBindingCommands),
  }))

  useBindings(() => ({
    enabled: () => renderer.currentFocusedEditor === null,
    bindings: tuiConfig.keybinds.gather("session.global.unfocused", sessionGlobalUnfocusedBindingCommands),
  }))

  useBindings(() => ({
    mode: OPENCODE_BASE_MODE,
    bindings: tuiConfig.keybinds.gather("session", sessionBindingCommands),
  }))

  useBindings(() => ({
    mode: OPENCODE_BASE_MODE,
    enabled: foregroundTasks().length > 0,
    priority: 1,
    bindings: tuiConfig.keybinds.get("session.background"),
  }))

  const revertInfo = createMemo(() => session()?.revert)
  const revertMessageID = createMemo(() => revertInfo()?.messageID)

  const revertDiffFiles = createMemo(() => getRevertDiffFiles(revertInfo()?.diff ?? ""))

  const revertRevertedMessages = createMemo(() => {
    const messageID = revertMessageID()
    if (!messageID) return []
    return messages().filter((x) => x.id >= messageID && x.role === "user")
  })

  const revert = createMemo(() => {
    const info = revertInfo()
    if (!info) return
    if (!info.messageID) return
    return {
      messageID: info.messageID,
      reverted: revertRevertedMessages(),
      diff: info.diff,
      diffFiles: revertDiffFiles(),
    }
  })

  // snap to bottom when session changes
  createEffect(on(() => route.sessionID, toBottom))

  return (
    <LocationProvider location={location()}>
      <context.Provider
        value={{
          get width() {
            return contentWidth()
          },
          sessionID: route.sessionID,
          conceal,
          thinkingMode,
          showThinking,
          showTimestamps,
          showDetails,
          showGenericToolOutput,
          diffWrapMode,
          providers,
          sync,
          tui: tuiConfig,
        }}
      >
        <box flexDirection="row" flexGrow={1} minHeight={0}>
          <box flexGrow={1} minHeight={0} paddingBottom={1} paddingLeft={2} paddingRight={2} gap={1}>
            <Show when={session()}>
              <scrollbox
                ref={(r) => (scroll = r)}
                viewportOptions={{
                  paddingRight: showScrollbar() ? 1 : 0,
                }}
                verticalScrollbarOptions={{
                  paddingLeft: 1,
                  visible: showScrollbar(),
                  trackOptions: {
                    backgroundColor: theme.backgroundElement,
                    foregroundColor: theme.border,
                  },
                }}
                stickyScroll={true}
                stickyStart="bottom"
                flexGrow={1}
                scrollAcceleration={scrollAcceleration()}
              >
                <box height={1} />
                <For each={messages()}>
                  {(message, index) => (
                    <Switch>
                      <Match when={message.id === revert()?.messageID}>
                        {(function () {
                          const redoShortcut = useCommandShortcut("session.redo")
                          const [hover, setHover] = createSignal(false)
                          const dialog = useDialog()

                          const handleUnrevert = async () => {
                            const confirmed = await DialogConfirm.show(
                              dialog,
                              "Confirm Redo",
                              "Are you sure you want to restore the reverted messages?",
                            )
                            if (confirmed) {
                              keymap.dispatchCommand("session.redo")
                            }
                          }

                          return (
                            <box
                              onMouseOver={() => setHover(true)}
                              onMouseOut={() => setHover(false)}
                              onMouseUp={handleUnrevert}
                              marginTop={1}
                              flexShrink={0}
                              border={["left"]}
                              customBorderChars={SplitBorder.customBorderChars}
                              borderColor={theme.backgroundPanel}
                            >
                              <box
                                paddingTop={1}
                                paddingBottom={1}
                                paddingLeft={2}
                                backgroundColor={hover() ? theme.backgroundElement : theme.backgroundPanel}
                              >
                                <text fg={theme.textMuted}>{revert()!.reverted.length} message reverted</text>
                                <text fg={theme.textMuted}>
                                  <span style={{ fg: theme.text }}>{redoShortcut()}</span> or /redo to restore
                                </text>
                                <Show when={revert()!.diffFiles?.length}>
                                  <box marginTop={1}>
                                    <For each={revert()!.diffFiles}>
                                      {(file) => (
                                        <text fg={theme.text}>
                                          {file.filename}
                                          <Show when={file.additions > 0}>
                                            <span style={{ fg: theme.diffAdded }}> +{file.additions}</span>
                                          </Show>
                                          <Show when={file.deletions > 0}>
                                            <span style={{ fg: theme.diffRemoved }}> -{file.deletions}</span>
                                          </Show>
                                        </text>
                                      )}
                                    </For>
                                  </box>
                                </Show>
                              </box>
                            </box>
                          )
                        })()}
                      </Match>
                      <Match when={revert()?.messageID && message.id >= revert()!.messageID}>
                        <></>
                      </Match>
                      <Match when={message.role === "user"}>
                        <UserMessage
                          index={index()}
                          onMouseUp={() => {
                            if (renderer.getSelection()?.getSelectedText()) return
                            dialog.replace(() => (
                              <DialogMessage
                                messageID={message.id}
                                sessionID={route.sessionID}
                                setPrompt={(promptInfo) => prompt?.set(promptInfo)}
                              />
                            ))
                          }}
                          message={message as UserMessage}
                          parts={sync.data.part[message.id] ?? []}
                          pending={pending()}
                        />
                      </Match>
                      <Match when={message.role === "assistant"}>
                        <AssistantMessage
                          last={lastAssistant()?.id === message.id}
                          message={message as AssistantMessage}
                          parts={sync.data.part[message.id] ?? []}
                        />
                      </Match>
                    </Switch>
                  )}
                </For>
                <Show when={showSessionSwarm()}>
                  <ArtemisSwarm />
                </Show>
              </scrollbox>
              <box flexShrink={0}>
                <Show when={permissions().length > 0}>
                  <PermissionPrompt
                    request={permissions()[0]}
                    directory={sync.session.get(permissions()[0].sessionID)?.directory}
                  />
                </Show>
                <Show when={permissions().length === 0 && questions().length > 0}>
                  <QuestionPrompt
                    request={questions()[0]}
                    directory={sync.session.get(questions()[0].sessionID)?.directory}
                  />
                </Show>
                <Show when={process.env.ARTEMIS === "1" && !session()?.parentID}>
                  <FlagConfirmBar />
                  <SwarmAgentFooter />
                </Show>
                <Show when={session()?.parentID}>
                  <SubagentFooter />
                </Show>
                <Show when={visible()}>
                  <pluginRuntime.Slot
                    name="session_prompt"
                    mode="replace"
                    session_id={route.sessionID}
                    visible={visible()}
                    disabled={disabled()}
                    on_submit={toBottom}
                    ref={bind}
                  >
                    <Prompt
                      visible={visible()}
                      ref={bind}
                      disabled={disabled()}
                      beforeSubmit={beforePromptSubmit}
                      onSubmit={() => {
                        toBottom()
                      }}
                      sessionID={route.sessionID}
                      right={<pluginRuntime.Slot name="session_prompt_right" session_id={route.sessionID} />}
                    />
                  </pluginRuntime.Slot>
                </Show>
              </box>
            </Show>
            <Toast />
          </box>
          <Show when={sidebarVisible()}>
            <Switch>
              <Match when={wide()}>
                <Sidebar sessionID={route.sessionID} />
              </Match>
              <Match when={!wide()}>
                <box
                  position="absolute"
                  top={0}
                  left={0}
                  right={0}
                  bottom={0}
                  alignItems="flex-end"
                  backgroundColor={RGBA.fromInts(0, 0, 0, 70)}
                >
                  <Sidebar sessionID={route.sessionID} />
                </box>
              </Match>
            </Switch>
          </Show>
        </box>
      </context.Provider>
    </LocationProvider>
  )
}

function UserMessage(props: {
  message: UserMessage
  parts: Part[]
  onMouseUp: () => void
  index: number
  pending?: string
}) {
  const ctx = use()
  const local = useLocal()
  const text = createMemo(() => {
    const texts = props.parts
      .map((x) => {
        if (x.type === "text" && !x.synthetic) {
          return x.text
        }
        return null
      })
      .filter(Boolean)
    return texts.join("\n\n")
  })
  const files = createMemo(() => props.parts.flatMap((x) => (x.type === "file" ? [x] : [])))
  const { theme } = useTheme()
  const [hover, setHover] = createSignal(false)
  const queued = createMemo(() => props.pending && props.message.id > props.pending)
  const color = createMemo(() => local.agent.color(props.message.agent))
  const queuedFg = createMemo(() => selectedForeground(theme, color()))
  const metadataVisible = createMemo(() => queued() || ctx.showTimestamps())

  const compaction = createMemo(() => props.parts.find((x) => x.type === "compaction"))

  return (
    <>
      <Show when={text()}>
        <box
          id={props.message.id}
          ref={(el: BoxRenderable) => alwaysSeparate.add(el)}
          border={["left"]}
          borderColor={color()}
          customBorderChars={SplitBorder.customBorderChars}
          marginTop={props.index === 0 ? 0 : 1}
        >
          <box
            onMouseOver={() => {
              setHover(true)
            }}
            onMouseOut={() => {
              setHover(false)
            }}
            onMouseUp={props.onMouseUp}
            paddingTop={1}
            paddingBottom={1}
            paddingLeft={2}
            backgroundColor={hover() ? theme.backgroundElement : theme.backgroundPanel}
            flexShrink={0}
          >
            <text fg={theme.text}>{text()}</text>
            <Show when={files().length}>
              <box flexDirection="row" paddingBottom={metadataVisible() ? 1 : 0} paddingTop={1} gap={1} flexWrap="wrap">
                <For each={files()}>
                  {(file) => {
                    const directory = file.mime === "application/x-directory"
                    return (
                      <text fg={theme.text}>
                        <span style={{ bg: theme.secondary, fg: theme.background }}>
                          {directory ? " Directory " : " File "}
                        </span>
                        <span style={{ bg: theme.backgroundElement, fg: theme.textMuted }}> {file.filename} </span>
                      </text>
                    )
                  }}
                </For>
              </box>
            </Show>
            <Show
              when={queued()}
              fallback={
                <Show when={ctx.showTimestamps()}>
                  <text fg={theme.textMuted}>
                    <span style={{ fg: theme.textMuted }}>
                      {Locale.todayTimeOrDateTime(props.message.time.created)}
                    </span>
                  </text>
                </Show>
              }
            >
              <text fg={theme.textMuted}>
                <span style={{ bg: color(), fg: queuedFg(), bold: true }}> QUEUED </span>
              </text>
            </Show>
          </box>
        </box>
      </Show>
      <Show when={compaction()}>
        <box
          marginTop={1}
          border={["top"]}
          title=" Compaction "
          titleAlignment="center"
          borderColor={theme.borderActive}
        />
      </Show>
    </>
  )
}

function AssistantMessage(props: { message: AssistantMessage; parts: Part[]; last: boolean }) {
  const ctx = use()
  const local = useLocal()
  const { theme } = useTheme()
  const sync = useSync()
  const messages = createMemo(() => sync.data.message[props.message.sessionID] ?? [])
  const model = createMemo(() => Model.name(ctx.providers(), props.message.providerID, props.message.modelID))

  const final = createMemo(() => {
    return props.message.finish && !["tool-calls", "unknown"].includes(props.message.finish)
  })

  const live = createMemo(
    () => props.last && !final() && props.message.error?.name !== "MessageAbortedError",
  )
  const [now, setNow] = createSignal(Date.now())
  createEffect(() => {
    if (!live()) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(id))
  })

  const duration = createMemo(() => {
    const user = messages().find((x) => x.role === "user" && x.id === props.message.parentID)
    if (!user || !user.time) return 0
    const end = props.message.time.completed ?? (live() ? now() : 0)
    if (!end) return 0
    return Math.max(0, end - user.time.created)
  })

  const childShortcut = useCommandShortcut("session.child.first")
  const backgroundShortcut = useCommandShortcut("session.background")

  return (
    <>
      <For each={props.parts}>
        {(part, index) => {
          const component = createMemo(() => PART_MAPPING[part.type as keyof typeof PART_MAPPING])
          return (
            <Show when={component()}>
              <Dynamic
                last={index() === props.parts.length - 1}
                component={component()}
                part={part as any}
                message={props.message}
              />
            </Show>
          )
        }}
      </For>
      <Show when={props.parts.some((x) => x.type === "tool" && x.tool === "task")}>
        <box paddingTop={1} paddingLeft={3}>
          <text fg={theme.text}>
            {childShortcut()}
            <span style={{ fg: theme.textMuted }}> view subagents</span>
            <Show
              when={
                sync.data.capabilities.experimentalBackgroundSubagents &&
                props.parts.some(
                  (x) =>
                    x.type === "tool" &&
                    x.tool === "task" &&
                    x.state.status === "running" &&
                    x.state.metadata?.background !== true,
                )
              }
            >
              <span style={{ fg: theme.textMuted }}> · </span>
              {backgroundShortcut()}
              <span style={{ fg: theme.textMuted }}> background</span>
            </Show>
          </text>
        </box>
      </Show>
      <Show when={props.message.error && props.message.error.name !== "MessageAbortedError"}>
        <box
          ref={(el: BoxRenderable) => alwaysSeparate.add(el)}
          border={["left"]}
          paddingTop={1}
          paddingBottom={1}
          paddingLeft={2}
          marginTop={1}
          backgroundColor={theme.backgroundPanel}
          customBorderChars={SplitBorder.customBorderChars}
          borderColor={theme.error}
        >
          <text fg={theme.textMuted}>{errorMessage(props.message.error)}</text>
        </box>
      </Show>
      <Switch>
        <Match when={props.last || final() || props.message.error?.name === "MessageAbortedError"}>
          <box ref={(el: BoxRenderable) => alwaysSeparate.add(el)} paddingLeft={3}>
            <text marginTop={1}>
              <span
                style={{
                  fg:
                    props.message.error?.name === "MessageAbortedError"
                      ? theme.textMuted
                      : local.agent.color(props.message.agent),
                }}
              >
                ▣{" "}
              </span>{" "}
              <span style={{ fg: theme.text }}>{Locale.titlecase(props.message.mode)}</span>
              <span style={{ fg: theme.textMuted }}> · {model()}</span>
              <Show when={duration() > 0 || live()}>
                <span style={{ fg: theme.textMuted }}> · {Locale.duration(duration())}</span>
              </Show>
              <Show when={props.message.error?.name === "MessageAbortedError"}>
                <span style={{ fg: theme.textMuted }}> · interrupted</span>
              </Show>
            </text>
          </box>
        </Match>
      </Switch>
    </>
  )
}

const PART_MAPPING = {
  text: TextPart,
  tool: ToolPart,
  reasoning: ReasoningPart,
}

const INLINE_TOOL_ICON_WIDTH = 2

function ReasoningPart(props: { last: boolean; part: ReasoningPart; message: AssistantMessage }) {
  const { theme } = useTheme()
  const ctx = use()
  // Collapsed by default in hide mode: a single line throughout, so the
  // layout never shifts. Click to open the full markdown block, click to close.
  const [expanded, setExpanded] = createSignal(false)

  const content = createMemo(() => {
    // OpenRouter encrypts some reasoning blocks; drop the placeholder.
    return props.part.text.replace("[REDACTED]", "").trim()
  })
  // Reasoning is finalized when the server sets `time.end` (see processor.ts).
  // Flips independently of the parent message completing.
  const isDone = createMemo(() => props.part.time.end !== undefined)
  const inMinimal = createMemo(() => ctx.thinkingMode() === "hide")
  const [now, setNow] = createSignal(Date.now())
  createEffect(() => {
    if (isDone()) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(id))
  })
  const duration = createMemo(() => {
    const end = props.part.time.end ?? now()
    return Math.max(0, end - props.part.time.start)
  })
  const summary = createMemo(() => reasoningSummary(content()))
  const syntax = createSyntaxStyleMemo(() => generateSubtleSyntax(theme))

  const toggle = () => {
    if (!inMinimal()) return
    setExpanded((prev) => !prev)
  }

  return (
    <Show when={process.env.ARTEMIS !== "1" && content()}>
      <box
        ref={(el: BoxRenderable) => alwaysSeparate.add(el)}
        paddingLeft={3}
        marginTop={1}
        flexDirection="column"
        flexShrink={0}
      >
        <box onMouseUp={toggle}>
          <ReasoningHeader
            toggleable={inMinimal()}
            open={!inMinimal() || expanded()}
            done={isDone()}
            title={summary().title}
            duration={isDone() || duration() > 0 ? Locale.duration(duration()) : undefined}
          />
        </box>
        <Show when={(!inMinimal() || expanded()) && summary().body}>
          <box paddingLeft={inMinimal() ? 2 : 0} marginTop={1}>
            <code
              filetype="markdown"
              drawUnstyledText={false}
              streaming={true}
              syntaxStyle={syntax()}
              content={summary().body}
              conceal={ctx.conceal()}
              fg={theme.textMuted}
            />
          </box>
        </Show>
      </box>
    </Show>
  )
}

function ReasoningHeader(props: {
  toggleable: boolean
  open: boolean
  done: boolean
  title: string | null
  duration?: string
}) {
  const { theme } = useTheme()
  const fg = () =>
    props.open
      ? RGBA.fromValues(theme.warning.r, theme.warning.g, theme.warning.b, theme.thinkingOpacity)
      : theme.warning

  return (
    <Switch>
      <Match when={!props.done}>
        <box flexDirection="row">
          <Spinner color={fg()}>{props.title ? "Thinking: " + props.title : "Thinking"}</Spinner>
        </box>
      </Match>
      <Match when={true}>
        <text fg={fg()} wrapMode="none">
          <Show when={props.toggleable}>
            <span>{props.open ? "- " : "+ "}</span>
          </Show>
          <span>Thought</span>
          <Show when={props.title || props.duration}>
            <span>: </span>
          </Show>
          <Show when={props.title}>
            <span>{props.title}</span>
          </Show>
          <Show when={props.duration}>
            <span>
              {props.title ? " · " : ""}
              {props.duration}
            </span>
          </Show>
        </text>
      </Match>
    </Switch>
  )
}

function TextPart(props: { last: boolean; part: TextPart; message: AssistantMessage }) {
  const ctx = use()
  const { theme, syntax } = useTheme()
  return (
    <Show when={props.part.text.trim()}>
      <box ref={(el: BoxRenderable) => alwaysSeparate.add(el)} paddingLeft={3} marginTop={1} flexShrink={0}>
        <markdown
          syntaxStyle={syntax()}
          streaming={true}
          internalBlockMode="top-level"
          content={props.part.text.trim()}
          tableOptions={{ style: "grid" }}
          conceal={ctx.conceal()}
          fg={theme.markdownText}
          bg={theme.background}
        />
      </box>
    </Show>
  )
}

// Pending messages moved to individual tool pending functions

function ToolPart(props: { last: boolean; part: ToolPart; message: AssistantMessage }) {
  const ctx = use()
  const display = createMemo(() => toolDisplay(props.part.tool))

  // Hide tool if showDetails is false and tool completed successfully.
  // Artemis solve card stays visible — ask_flags completes immediately while
  // the swarm still streams into it.
  const shouldHide = createMemo(() => {
    if (ctx.showDetails()) return false
    if (props.part.state.status !== "completed") return false
    if (props.part.tool === "artemis_ask_flags" || props.part.tool === "artemis_swarm") return false
    return true
  })

  const toolprops = {
    get metadata() {
      return props.part.state.status === "pending" ? {} : (props.part.state.metadata ?? {})
    },
    get input() {
      return props.part.state.input ?? {}
    },
    get output() {
      return props.part.state.status === "completed" ? props.part.state.output : undefined
    },
    get tool() {
      return props.part.tool
    },
    get part() {
      return props.part
    },
  }

  return (
    <Show when={!shouldHide()}>
      <Switch>
        <Match when={display() === "artemis_swarm"}>
          <ArtemisSwarm {...toolprops} />
        </Match>
        <Match when={display() === "bash"}>
          <Shell {...toolprops} />
        </Match>
        <Match when={display() === "glob"}>
          <Glob {...toolprops} />
        </Match>
        <Match when={display() === "read"}>
          <Read {...toolprops} />
        </Match>
        <Match when={display() === "grep"}>
          <Grep {...toolprops} />
        </Match>
        <Match when={display() === "webfetch"}>
          <WebFetch {...toolprops} />
        </Match>
        <Match when={display() === "websearch"}>
          <WebSearch {...toolprops} />
        </Match>
        <Match when={display() === "write"}>
          <Write {...toolprops} />
        </Match>
        <Match when={display() === "edit"}>
          <Edit {...toolprops} />
        </Match>
        <Match when={display() === "task"}>
          <Task {...toolprops} />
        </Match>
        <Match when={display() === "execute"}>
          <Execute {...toolprops} />
        </Match>
        <Match when={display() === "apply_patch"}>
          <ApplyPatch {...toolprops} />
        </Match>
        <Match when={display() === "todowrite"}>
          <TodoWrite {...toolprops} />
        </Match>
        <Match when={display() === "question"}>
          <Question {...toolprops} />
        </Match>
        <Match when={display() === "skill"}>
          <Skill {...toolprops} />
        </Match>
        <Match when={true}>
          <GenericTool {...toolprops} />
        </Match>
      </Switch>
    </Show>
  )
}

type ToolProps = {
  input: Record<string, unknown>
  metadata: Record<string, unknown>
  tool: string
  output?: string
  part: ToolPart
}
type ToolGroupItem = { kind: "bash"; command: string } | { kind: "tool"; tool: string; detail: string } | { kind: "result"; text: string }
type ToolGroup = { type: "tools"; agent: string; items: ToolGroupItem[]; running: boolean }
type SwarmGroup =
  | ToolGroup
  | { type: "think"; text: string; agent?: string }
  | { type: "ai"; text: string; agent?: string }
  | { type: "flags_ask"; default: number; challenge?: string }
  | { type: "outcome"; level: string; text: string }
  | { type: "flag_confirm"; flag: string }
  | { type: "status"; text: string }
  | { type: "summary"; lines: string[] }

/** Group consecutive bash/tool/result events into one tool card per agent. */
function groupSwarmEvents(events: ArtemisEvent[], swarmRunning: boolean): SwarmGroup[] {
  const out: SwarmGroup[] = []
  let cur: ToolGroup | null = null
  const seenOutcome = new Set<string>()
  const flush = () => {
    if (cur) {
      out.push(cur)
      cur = null
    }
  }
  const outcomeKey = (text: string) =>
    text
      .replace(/^\[artemis\]\s+outcome\s+/i, "")
      .replace(/^>>>\s*/i, "")
      .replace(/\s*\(resets?\s+[^)]+\)/gi, "")
      .replace(/\s*\(\d{1,2}\/\d{1,2}(?:\/\d{2,4})?\)/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase()
  for (const ev of events) {
    if (ev.kind === "bash" || ev.kind === "tool" || ev.kind === "result") {
      if (!cur || cur.agent !== ev.agent) {
        flush()
        cur = { type: "tools", agent: ev.agent, items: [], running: true }
      }
      if (ev.kind === "bash") cur.items.push({ kind: "bash", command: ev.command })
      else if (ev.kind === "tool") cur.items.push({ kind: "tool", tool: ev.tool, detail: ev.detail })
      else cur.items.push({ kind: "result", text: ev.text })
    } else {
      flush()
      if (ev.kind === "think") out.push({ type: "think", text: ev.text, agent: ev.agent })
      else if (ev.kind === "ai") out.push({ type: "ai", text: ev.text, agent: ev.agent })
      else if (ev.kind === "flags_ask") out.push({ type: "flags_ask", default: ev.default, challenge: ev.challenge })
      else if (ev.kind === "outcome") {
        const key = outcomeKey(ev.text)
        if (seenOutcome.has(key)) continue
        seenOutcome.add(key)
        out.push({ type: "outcome", level: ev.level, text: ev.text })
      } else if (ev.kind === "flag_confirm") out.push({ type: "flag_confirm", flag: ev.flag })
      else if (ev.kind === "summary") {
        const last = out[out.length - 1]
        // Consecutive recap lines are one block, not one group per line.
        if (last?.type === "summary") last.lines.push(ev.text)
        else out.push({ type: "summary", lines: [ev.text] })
      } else if (ev.kind === "status") {
        const key = outcomeKey(ev.text)
        if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text) && seenOutcome.has(key))
          continue
        if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text)) seenOutcome.add(key)
        out.push({ type: "status", text: ev.text })
      }
    }
  }
  flush()
  if (out.length > 0 && swarmRunning) {
    const last = out[out.length - 1]
    if (last.type === "tools") {
      for (let i = 0; i < out.length - 1; i++) {
        if (out[i].type === "tools") (out[i] as ToolGroup).running = false
      }
    }
  } else {
    for (const g of out) {
      if (g.type === "tools") g.running = false
    }
  }
  return out
}

function SwarmToolCard(props: { group: ToolGroup; running: boolean; part?: ToolPart }) {
  const { theme } = useTheme()
  const ctx = use()
  const [expanded, setExpanded] = createSignal(true)
  const isLast = createMemo(() => props.group.running)
  // Auto-collapse when the swarm is no longer running (a later group exists).
  createEffect(() => {
    if (!props.running) setExpanded(false)
  })
  const summary = createMemo(() => {
    const last = props.group.items[props.group.items.length - 1]
    if (!last) return props.group.agent
    if (last.kind === "bash") return `$ ${last.command}`
    if (last.kind === "tool") {
      const label =
        last.tool === "read_file" ? "Read"
          : last.tool === "write_file" ? "Write"
            : last.tool === "list_files" ? "List"
              : last.tool === "submit_flag" ? "Submit" : last.tool
      return `${label} ${last.detail}`
    }
    return `↳ ${last.text}`
  })
  const maxLines = createMemo(() => (props.running ? 12 : 6))
  const maxChars = createMemo(() => maxLines() * Math.max(20, ctx.width - 6))
  const allText = createMemo(() =>
    props.group.items
      .map((it) => (it.kind === "bash" ? `$ ${it.command}` : it.kind === "tool" ? `${it.tool} ${it.detail}` : `↳ ${it.text}`))
      .join("\n"),
  )
  const collapsed = createMemo(() => collapseToolOutput(allText(), maxLines(), maxChars()))
  const limited = createMemo(() => (expanded() || !collapsed().overflow ? allText() : collapsed().output))

  return (
    <BlockTool
      title={props.group.agent}
      part={props.part}
      spinner={props.running && isLast()}
      onClick={() => setExpanded((p) => !p)}
    >
      <box gap={1}>
        <Show when={props.running && isLast()}>
          <Spinner color={theme.text}>live</Spinner>
        </Show>
        <Show when={expanded()}>
          <text fg={theme.text} wrapMode="char">
            {limited()}
          </text>
        </Show>
        <Show when={!expanded()}>
          <text fg={theme.textMuted} wrapMode="char">
            {summary()}
          </text>
        </Show>
        <Show when={collapsed().overflow || !expanded()}>
          <text fg={theme.textMuted}>{expanded() ? "Click to collapse" : "Click to expand"}</text>
        </Show>
      </box>
    </BlockTool>
  )
}

function ArtemisSwarm(props: { part?: ToolPart }) {
  const { theme } = useTheme()
  const events = createMemo(() => daemon.swarmEvents[0]())
  const agents = createMemo(() =>
    listSwarmAgents(events(), daemon.lastModels[0](), daemon.swarmRoster[0]()),
  )
  const multiAgent = createMemo(() => agents().length > 1)
  const processAlive = createMemo(() => daemon.swarmRunning[0]())
  const terminalDone = createMemo(
    () => hasTerminalSolveOutcome(events(), multiAgent()) || daemon.flowCompleted[0](),
  )
  // Solving UI (spinner / boot) stops at terminal outcome; process may still
  // tear down until swarm_exit — keep processAlive for grid/timer/Esc.
  const isRunning = createMemo(
    () =>
      !terminalDone() &&
      (props.part?.state.status === "running" ||
        props.part?.state.status === "pending" ||
        processAlive()),
  )
  const agentStarted = createMemo(() => hasAgentActivity(events()))
  const focus = createMemo(() => (multiAgent() ? daemon.swarmFocus[0]() : null))

  const bootLines = createMemo(() =>
    events()
      .filter((ev) => ev.kind === "boot" || ev.kind === "flags_ask" || (ev.kind === "status" && !agentStarted()))
      // On an agent page show only that container's boot, plus shared lines.
      .filter((ev) => !(ev.kind === "boot" && ev.agent && focus() && ev.agent !== focus()))
      .map((ev) => {
        if (ev.kind === "flags_ask") {
          return `Flags required? (default ${ev.default}${ev.challenge ? ` · ${ev.challenge}` : ""})`
        }
        if (ev.kind === "boot" || ev.kind === "status") return ev.text
        return ""
      })
      .filter(Boolean),
  )
  const showBoot = createMemo(() => !agentStarted())
  const visible = createMemo(() => {
    const list = events()
      .map((ev) => {
        if (ev.kind === "result") return { ...ev, text: ev.text.length > 4000 ? ev.text.slice(0, 3999) + "…" : ev.text }
        return ev
      })
      .filter((ev) => {
        if (ev.kind === "turn") return false
        if (ev.kind === "boot") return false
        if (ev.kind === "status") {
          return /Waiting for flag|Confirmed|Rejected|swarm exit|Possible flag|submit_flag/i.test(ev.text)
        }
        if (ev.kind === "flags_ask") return !agentStarted()
        return true
      })
    return agentStarted() ? list.filter((ev) => ev.kind !== "flags_ask") : list
  })

  const feedEvents = createMemo(() => {
    const f = focus()
    let list = visible()
    if (!f) {
      list = multiAgent() ? globalSwarmEvents(list) : list
    } else {
      list = partitionEventsByAgent(list, f)
    }
    return dropPostSolveAgentChatter(list)
  })
  const emptyAgentHint = createMemo(() => {
    if (!focus()) return null
    if (feedEvents().length > 0) return null
    // Only after process exit — soft-race per-agent quota must not brand siblings.
    if (!processAlive() && hasGlobalQuotaOutcome(events())) {
      return "usage limit · this agent never started"
    }
    if (!processAlive()) return "No activity from this agent"
    return null
  })
  const groupedEvents = createMemo(() => groupSwarmEvents(feedEvents(), isRunning()))
  // Use full event stream for failure/quota detection (visible() filters status lines).
  // lineCounts stay monotonic even when coalesce shortens the event list.
  // Pass processAlive (not Solving UI) so not-started siblings stay starting…
  // until swarm_exit — matches DoD not-started ≠ no-quota.
  const previews = createMemo(() =>
    agentPreviews(events(), agents(), processAlive(), daemon.agentLineCounts[0]()),
  )
  // Multi-agent: show boxes as soon as roster/models known (including boot).
  const showGrid = createMemo(() => multiAgent() && !focus())
  /** Hide the empty "# Artemis / No solver activity" card when nothing started. */
  const hasSolveUi = createMemo(() =>
    swarmHasVisibleSolveUi({
      running: processAlive() || isRunning(),
      eventCount: events().length,
      startedAt: daemon.swarmStartedAt[0](),
      agentStarted: agentStarted(),
      showGrid: showGrid(),
    }),
  )

  const [now, setNow] = createSignal(Date.now())
  createEffect(() => {
    if (!processAlive()) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(id))
  })
  // Windows / reconnect: footer shows Solving but Solve card missed daemon pushes.
  createEffect(() => {
    if (!processAlive()) return
    if (bootLines().length > 0 || agentStarted()) return
    const t = setTimeout(() => {
      if (!processAlive()) return
      if (bootLines().length > 0 || agentStarted()) return
      daemon.scheduleSwarmReplayIfEmpty()
    }, 1200)
    onCleanup(() => clearTimeout(t))
  })
  const elapsedLabel = createMemo(() => {
    const started = daemon.swarmStartedAt[0]()
    if (started == null) return null
    const end = processAlive() ? now() : (daemon.swarmEndedAt[0]() ?? now())
    const secs = Math.max(0, Math.floor((end - started) / 1000))
    // Integer seconds while live (1s, 2s, …); keep showing after exit.
    if (secs < 60) return `${secs}s`
    return formatDuration(secs) || `${secs}s`
  })

  const outcomeColor = (level: string) => {
    if (level === "success") return theme.success
    if (level === "warn") return theme.warning
    if (level === "error") return theme.error
    return theme.textMuted
  }

  const renderGroups = () => (
    <For each={groupedEvents()}>
      {(group) => (
        <Switch>
          <Match when={group.type === "flags_ask"}>
            <box paddingLeft={3} flexShrink={0}>
              <text fg={theme.warning}>
                ?# Flags required? (default {(group as { default: number; challenge?: string }).default}
                {(group as { challenge?: string }).challenge ? ` · ${(group as { challenge?: string }).challenge}` : ""})
              </text>
            </box>
          </Match>
          <Match when={group.type === "think"}>
            <box paddingLeft={3} flexDirection="column" flexShrink={0} gap={0}>
              <text fg={theme.textMuted} wrapMode="char">
                {(group as { text: string }).text}
              </text>
              <text fg={theme.textMuted}>Cogitated</text>
            </box>
          </Match>
          <Match when={group.type === "ai"}>
            <box paddingLeft={3} flexShrink={0}>
              <text fg={theme.text} wrapMode="char">
                {(group as { text: string }).text}
              </text>
            </box>
          </Match>
          <Match when={group.type === "tools"}>
            <SwarmToolCard group={group as ToolGroup} running={(group as ToolGroup).running} part={props.part} />
          </Match>
          <Match when={group.type === "outcome"}>
            <box paddingLeft={3} flexShrink={0}>
              <text fg={outcomeColor((group as { level: string }).level)} attributes={TextAttributes.BOLD} wrapMode="char">
                {(group as { level: string }).level === "success" ? "✓ " : (group as { level: string }).level === "warn" ? "! " : ""}
                {(group as { text: string }).text}
              </text>
            </box>
          </Match>
          <Match when={group.type === "flag_confirm"}>
            <box paddingLeft={3} flexShrink={0}>
              <text fg={theme.warning} wrapMode="char">
                ⚑ Confirm flag? {(group as { flag: string }).flag}
              </text>
            </box>
          </Match>
          <Match when={group.type === "status"}>
            <box paddingLeft={3} flexShrink={0}>
              <text fg={theme.textMuted} wrapMode="char">
                {(group as { text: string }).text}
              </text>
            </box>
          </Match>
          <Match when={group.type === "summary"}>
            <box
              paddingLeft={3}
              paddingRight={1}
              flexDirection="column"
              flexShrink={0}
              gap={0}
              marginTop={1}
              width="100%"
            >
              <Show
                when={(group as { lines: string[] }).lines.some((line) => {
                  const t = line.trim()
                  if (!t || /no writeup recorded|Writing recap/i.test(t)) return false
                  if (/^(How|Solved by|Solved ·|Flag)\b/i.test(t)) return false
                  return (
                    /^\d+\.\s/.test(t) ||
                    (/^(Challenge|Key insight|Solution summary|What I tried|Why it worked|Dead ends)\b/i.test(t)) ||
                    t.length >= 80
                  )
                })}
              >
                <text fg={theme.success} attributes={TextAttributes.BOLD} wrapMode="word">
                  How the flag was found
                </text>
              </Show>
              <For each={(group as { lines: string[] }).lines}>
                {(line) => {
                  const t = line.trim()
                  const recapLines = (group as { lines: string[] }).lines
                  const hasRecapBody = recapLines.some((raw) => {
                    const s = raw.trim()
                    if (!s || /no writeup recorded|Writing recap/i.test(s)) return false
                    if (/^(How|Solved by|Solved ·|Flag)\b/i.test(s)) return false
                    return (
                      /^\d+\.\s/.test(s) ||
                      (/^(Challenge|Key insight|Solution summary|What I tried|Why it worked|Dead ends)\b/i.test(s)) ||
                      s.length >= 80
                    )
                  })
                  const howSettled = recapLines.some((raw) => {
                    const s = raw.trim()
                    return /^How\b/i.test(s) || /no writeup recorded/i.test(s)
                  })
                  const hidePending = /Writing recap/i.test(t) && (hasRecapBody || howSettled)
                  // Title already says "How the flag was found" — drop leftover How: labels
                  // (interim + final both used to print one).
                  const hideHowLabel =
                    hasRecapBody && /^How(?:\s*\(.*\))?\s*:?\s*$/i.test(t)
                  const isSolvedBy = /^Solved by /i.test(t)
                  const isHeader =
                    /^(How|Challenge|Key insight|Flag|Solution summary|Steps|What I tried|Why it worked|Dead ends)\b/i.test(t) ||
                    /^Flag:/i.test(t)
                  const isStep = /^\d+\.\s/.test(t)
                  const fg = isSolvedBy || isHeader
                    ? theme.success
                    : isStep
                      ? theme.textMuted
                      : theme.text
                  const attrs = isSolvedBy || isHeader ? TextAttributes.BOLD : undefined
                  return (
                    <Show when={!hidePending && !hideHowLabel}>
                      <text fg={fg} attributes={attrs} wrapMode="word">
                        {line}
                      </text>
                    </Show>
                  )
                }}
              </For>
            </box>
          </Match>
        </Switch>
      )}
    </For>
  )

  return (
    <Show when={hasSolveUi()}>
    <box gap={1} flexShrink={0} marginTop={1}>
      {/* Scrollback marker only. The live spinner, elapsed and page nav are in
          the sticky footer, so this stays static to avoid a second animation. */}
      <box paddingLeft={3} flexDirection="row" gap={1}>
        <text fg={theme.textMuted}>
          {isRunning() ? (showBoot() ? "Starting…" : "Solving") : "Solve"}
        </text>
        <Show when={elapsedLabel()}>
          <text fg={theme.textMuted}>· {elapsedLabel()}</text>
        </Show>
      </box>

      <Show when={showBoot() && !showGrid()}>
        <BlockTool title="# Artemis" part={props.part} spinner={isRunning()}>
          <box gap={0} paddingLeft={1}>
            <For each={bootLines().length ? bootLines() : isRunning() ? [
              daemon.sessionState[0]().challenge_name
                ? `Loaded ${daemon.sessionState[0]().challenge_name}`
                : "Waiting for solvers…",
              ...(daemon.sessionState[0]().challenge_name ? ["Waiting for solvers…"] : []),
            ] : ["No solver activity"]}>
              {(line) => (
                <text fg={theme.textMuted} wrapMode="char">
                  {line}
                </text>
              )}
            </For>
          </box>
        </BlockTool>
      </Show>

      <Show when={showBoot() && showGrid()}>
        <box paddingLeft={3} flexShrink={0}>
          <text fg={theme.textMuted} wrapMode="char">
            {bootLines().slice(0, 3).join(" · ") || "Starting swarm…"}
          </text>
        </box>
      </Show>

      <Show when={showGrid()}>
        <For each={previews()}>
          {(p) => (
            <BlockTool
              title={p.agent}
              part={props.part}
              spinner={!p.won && (p.active || (processAlive() && p.eventCount === 0 && !p.failed))}
              onClick={() => {
                const i = agents().indexOf(p.agent)
                daemon.setSwarmNavCursor(i >= 0 ? i + 1 : 0)
                daemon.setSwarmFocus(p.agent)
              }}
            >
              <box gap={0}>
                <Show when={p.won}>
                  <text fg={theme.success} attributes={TextAttributes.BOLD}>
                    ✓ solved this challenge
                  </text>
                </Show>
                <Show when={p.failed}>
                  <text fg={theme.error}>failed</text>
                </Show>
                <Show when={!p.won && !p.failed && (p.active || (processAlive() && p.eventCount === 0))}>
                  <Spinner color={theme.text}>{p.eventCount === 0 ? "starting" : "live"}</Spinner>
                </Show>
                <text fg={p.won ? theme.success : p.failed ? theme.error : theme.textMuted} wrapMode="char">
                  {p.lastLine}
                </text>
                {/* No elapsed here — it is run-wide, not per agent. */}
                <text fg={theme.textMuted}>{p.eventCount} events · click to open</text>
              </box>
            </BlockTool>
          )}
        </For>
        {/* Always show global recap on main when finished — even if activity was wiped. */}
        <Show when={agentStarted() || terminalDone()}>{renderGroups()}</Show>
      </Show>

      <Show when={!multiAgent() || focus()}>
        <Show when={emptyAgentHint()}>
          <box paddingLeft={3} flexShrink={0}>
            <text fg={theme.error} wrapMode="char">
              {emptyAgentHint()}
            </text>
          </box>
        </Show>
        <Show when={agentStarted() || focus() || terminalDone()}>{renderGroups()}</Show>
      </Show>
    </box>
    </Show>
  )
}

function GenericTool(props: ToolProps) {
  const { theme } = useTheme()
  const ctx = use()
  const isRunning = createMemo(() => props.part.state.status === "running" || props.part.state.status === "pending")
  const isArtemis = createMemo(() => props.tool.startsWith("artemis_"))
  const liveMeta = createMemo(() => stripAnsi(stringValue(props.metadata.output)?.trim() ?? ""))
  const completed = createMemo(() => props.output?.trim() ?? "")
  const output = createMemo(() => liveMeta() || completed() || "")
  const [expanded, setExpanded] = createSignal(isArtemis())
  const maxLines = createMemo(() => (isRunning() ? 16 : isArtemis() ? 8 : 3))
  const maxChars = createMemo(() => maxLines() * Math.max(20, ctx.width - 6))
  const collapsed = createMemo(() => collapseToolOutput(output(), maxLines(), maxChars()))
  const limited = createMemo(() => {
    if (expanded() || !collapsed().overflow) return output()
    return collapsed().output
  })
  const showBlock = createMemo(
    () => Boolean(output()) && (isRunning() || ctx.showGenericToolOutput() || isArtemis()),
  )
  const blockTitle = createMemo(() => {
    if (props.tool === "artemis_load_challenge") return "Load challenge"
    if (props.tool === "artemis_status") return "Status"
    if (props.tool.startsWith("artemis_")) return props.tool.replace(/^artemis_/, "").replaceAll("_", " ")
    return `# ${props.tool} ${input(props.input)}`
  })

  return (
    <Show
      when={showBlock()}
      fallback={
        <InlineTool
          icon="⚙"
          pending="Working..."
          complete={!isRunning()}
          spinner={isRunning()}
          part={props.part}
        >
          {blockTitle()}
        </InlineTool>
      }
    >
      <BlockTool
        title={blockTitle()}
        part={props.part}
        onClick={collapsed().overflow ? () => setExpanded((prev) => !prev) : undefined}
      >
        <box gap={1}>
          <Show when={isRunning()}>
            <Spinner color={theme.text}>live</Spinner>
          </Show>
          <Show when={output()}>
            <text fg={theme.text}>{limited()}</text>
          </Show>
          <Show when={collapsed().overflow}>
            <text fg={theme.textMuted}>{expanded() ? "Click to collapse" : "Click to expand"}</text>
          </Show>
        </box>
      </BlockTool>
    </Show>
  )
}

function InlineTool(props: {
  icon: string
  iconColor?: RGBA
  color?: RGBA
  complete: unknown
  pending: string
  failure?: string
  spinner?: boolean
  separate?: boolean
  children: JSX.Element
  part: ToolPart
  onClick?: () => void
}) {
  const { theme } = useTheme()
  const ctx = use()
  const sync = useSync()
  const renderer = useRenderer()
  const [hover, setHover] = createSignal(false)
  const [errorExpanded, setErrorExpanded] = createSignal(false)

  const permission = createMemo(() => {
    const callID = sync.data.permission[ctx.sessionID]?.at(0)?.tool?.callID
    if (!callID) return false
    return callID === props.part.callID
  })

  const error = createMemo(() => (props.part.state.status === "error" ? props.part.state.error : undefined))

  const denied = createMemo(
    () =>
      error()?.includes("QuestionRejectedError") ||
      error()?.includes("rejected permission") ||
      error()?.includes("specified a rule") ||
      error()?.includes("user dismissed"),
  )

  const failed = createMemo(() => Boolean(error() && !denied()))
  const clickable = createMemo(() => Boolean(props.onClick || failed()))
  const fg = createMemo(() => {
    if (props.color) return props.color
    if (permission()) return theme.warning
    if (failed()) return theme.error
    if (hover() && props.onClick) return theme.text
    if (props.complete) return theme.textMuted
    return theme.text
  })

  return (
    <InlineToolRow
      icon={props.icon}
      iconColor={props.iconColor}
      color={fg()}
      errorColor={theme.error}
      failed={failed()}
      denied={Boolean(denied())}
      error={error()}
      errorExpanded={errorExpanded()}
      complete={props.complete}
      pending={props.pending}
      failure={props.failure}
      spinner={props.spinner}
      separate={props.separate}
      onMouseOver={() => clickable() && setHover(true)}
      onMouseOut={() => setHover(false)}
      onMouseUp={() => {
        if (renderer.getSelection()?.getSelectedText()) return
        if (failed()) {
          setErrorExpanded((value) => !value)
          return
        }
        props.onClick?.()
      }}
    >
      {props.children}
    </InlineToolRow>
  )
}

export function InlineToolRow(props: {
  icon: string
  iconColor?: RGBA
  color?: RGBA
  errorColor?: RGBA
  failed?: boolean
  denied?: boolean
  error?: string
  errorExpanded?: boolean
  complete: unknown
  pending: string
  failure?: string
  spinner?: boolean
  separate?: boolean
  children: JSX.Element
  onMouseOver?: () => void
  onMouseOut?: () => void
  onMouseUp?: () => void
}) {
  return (
    <box
      paddingLeft={3}
      onMouseOver={props.onMouseOver}
      onMouseOut={props.onMouseOut}
      onMouseUp={props.onMouseUp}
      ref={(el: BoxRenderable) => {
        if (props.separate) alwaysSeparate.add(el)
        setPreLayoutSiblingMargin(el, (previous) => {
          return props.separate ||
            (previous instanceof BoxRenderable && (previous.height > 1 || alwaysSeparate.has(previous)))
            ? 1
            : 0
        })
      }}
    >
      <Switch>
        <Match when={props.spinner}>
          <Spinner color={props.color} children={props.children} />
        </Match>
        <Match when={true}>
          <Show
            fallback={
              <text
                paddingLeft={3}
                fg={props.color}
                attributes={props.denied ? TextAttributes.STRIKETHROUGH : undefined}
              >
                ~ {props.pending}
              </text>
            }
            when={props.complete || props.failed}
          >
            <box flexDirection="row">
              <text
                width={INLINE_TOOL_ICON_WIDTH}
                fg={props.failed ? props.errorColor : (props.iconColor ?? props.color)}
                attributes={props.denied ? TextAttributes.STRIKETHROUGH : undefined}
              >
                {props.icon}
              </text>
              <text
                flexGrow={1}
                fg={props.failed ? props.errorColor : props.color}
                attributes={props.denied ? TextAttributes.STRIKETHROUGH : undefined}
              >
                {props.failed && !props.complete ? (props.failure ?? props.children) : props.children}
              </text>
            </box>
          </Show>
        </Match>
      </Switch>
      <Show when={props.failed && props.errorExpanded}>
        <box paddingLeft={INLINE_TOOL_ICON_WIDTH}>
          <text fg={props.errorColor}>{props.error}</text>
        </box>
      </Show>
    </box>
  )
}

function BlockTool(props: {
  title?: string
  children: JSX.Element
  onClick?: () => void
  part?: ToolPart
  spinner?: boolean
}) {
  const { theme } = useTheme()
  const renderer = useRenderer()
  const [hover, setHover] = createSignal(false)
  const error = createMemo(() => (props.part?.state.status === "error" ? props.part.state.error : undefined))
  return (
    <box
      ref={(el: BoxRenderable) => alwaysSeparate.add(el)}
      border={["left"]}
      paddingTop={1}
      paddingBottom={1}
      paddingLeft={2}
      marginTop={1}
      gap={1}
      backgroundColor={hover() ? theme.backgroundMenu : theme.backgroundPanel}
      customBorderChars={SplitBorder.customBorderChars}
      borderColor={theme.background}
      onMouseOver={() => props.onClick && setHover(true)}
      onMouseOut={() => setHover(false)}
      onMouseUp={() => {
        if (renderer.getSelection()?.getSelectedText()) return
        props.onClick?.()
      }}
    >
      <Show when={props.title}>
        {(title) => (
          <Show
            when={props.spinner}
            fallback={
              <text paddingLeft={3} fg={theme.textMuted}>
                {title()}
              </text>
            }
          >
            <Spinner color={theme.textMuted}>{title().replace(/^# /, "")}</Spinner>
          </Show>
        )}
      </Show>
      {props.children}
      <Show when={error()}>
        <text fg={theme.error}>{error()}</text>
      </Show>
    </box>
  )
}

function Shell(props: ToolProps) {
  const { theme } = useTheme()
  const pathFormatter = usePathFormatter()
  const ctx = use()
  const isRunning = createMemo(() => props.part.state.status === "running")
  const output = createMemo(() => {
    const meta = stripAnsi(stringValue(props.metadata.output)?.trim() ?? "")
    if (meta) return meta
    // Artemis bash returns final text in part.state.output (no metadata.output stream)
    return stripAnsi(props.output?.trim() ?? "")
  })
  const [expanded, setExpanded] = createSignal(false)
  const maxLines = 10
  const maxChars = createMemo(() => maxLines * Math.max(20, ctx.width - 6))
  const collapsed = createMemo(() => collapseToolOutput(output(), maxLines, maxChars()))
  const limited = createMemo(() => {
    if (expanded() || !collapsed().overflow) return output()
    return collapsed().output
  })

  const workdirDisplay = createMemo(() => {
    const workdir = stringValue(props.input.workdir)
    if (!workdir || workdir === ".") return undefined
    const formatted = pathFormatter.format(workdir)
    if (formatted === ".") return undefined
    return formatted
  })

  const title = createMemo(() => {
    const wd = workdirDisplay()
    if (!wd) return
    return `# Running in ${wd}`
  })

  return (
    <Switch>
      <Match when={output() || isRunning()}>
        <BlockTool
          title={title()}
          part={props.part}
          onClick={collapsed().overflow ? () => setExpanded((prev) => !prev) : undefined}
        >
          <box gap={1}>
            <Show when={isRunning()} fallback={<text fg={theme.text}>$ {stringValue(props.input.command)}</text>}>
              <Spinner color={theme.text}>{stringValue(props.input.command)}</Spinner>
            </Show>
            <Show when={output()}>
              <text fg={theme.text}>{limited()}</text>
            </Show>
            <Show when={collapsed().overflow}>
              <text fg={theme.textMuted}>{expanded() ? "Click to collapse" : "Click to expand"}</text>
            </Show>
          </box>
        </BlockTool>
      </Match>
      <Match when={true}>
        <InlineTool icon="$" pending="Writing command..." complete={stringValue(props.input.command)} part={props.part}>
          {stringValue(props.input.command)}
        </InlineTool>
      </Match>
    </Switch>
  )
}

function Write(props: ToolProps) {
  const { theme, syntax } = useTheme()
  const pathFormatter = usePathFormatter()
  const filePath = createMemo(
    () => stringValue(props.input.filePath) ?? stringValue(props.input.path) ?? stringValue(props.input.filename),
  )
  const code = createMemo(() => {
    return stringValue(props.input.content) ?? ""
  })
  const showBody = createMemo(() => Boolean(code()) || props.metadata.diagnostics !== undefined)

  return (
    <Switch>
      <Match when={showBody()}>
        <BlockTool title={"# Wrote " + pathFormatter.format(filePath())} part={props.part}>
          <Show when={code()}>
            <line_number fg={theme.textMuted} minWidth={3} paddingRight={1}>
              <code
                conceal={false}
                fg={theme.text}
                filetype={filetype(filePath())}
                syntaxStyle={syntax()}
                content={code()}
              />
            </line_number>
          </Show>
          <Show when={props.metadata.diagnostics !== undefined}>
            <Diagnostics diagnostics={props.metadata.diagnostics} filePath={filePath() ?? ""} />
          </Show>
        </BlockTool>
      </Match>
      <Match when={true}>
        <InlineTool icon="←" pending="Preparing write..." complete={filePath()} part={props.part}>
          Write {pathFormatter.format(filePath())}
        </InlineTool>
      </Match>
    </Switch>
  )
}

function Glob(props: ToolProps) {
  const pathFormatter = usePathFormatter()
  const pattern = createMemo(
    () => stringValue(props.input.pattern) ?? stringValue(props.input.path) ?? stringValue(props.input.glob),
  )
  return (
    <InlineTool icon="✱" pending="Finding files..." complete={pattern()} part={props.part}>
      <>Glob "{pattern()}" </>
      <Show when={stringValue(props.input.path)}>
        in {pathFormatter.format(stringValue(props.input.path))}{" "}
      </Show>
      <Show when={numberValue(props.metadata.count)}>
        ({numberValue(props.metadata.count)} {numberValue(props.metadata.count) === 1 ? "match" : "matches"})
      </Show>
    </InlineTool>
  )
}

function Read(props: ToolProps) {
  const { theme } = useTheme()
  const pathFormatter = usePathFormatter()
  const isRunning = createMemo(() => props.part.state.status === "running")
  const filePath = createMemo(
    () => stringValue(props.input.filePath) ?? stringValue(props.input.path) ?? stringValue(props.input.filename),
  )
  const loaded = createMemo(() => {
    if (props.part.state.status !== "completed") return []
    if (props.part.state.time.compacted) return []
    const value = props.metadata.loaded
    if (!value || !Array.isArray(value)) return []
    return value.filter((p): p is string => typeof p === "string")
  })
  return (
    <>
      <InlineTool
        icon="→"
        pending="Reading file..."
        complete={filePath()}
        spinner={isRunning()}
        part={props.part}
      >
        Read {pathFormatter.format(filePath())} {input(props.input, ["filePath", "path", "filename"])}
      </InlineTool>
      <For each={loaded()}>
        {(filepath) => (
          <box paddingLeft={3}>
            <text paddingLeft={3} fg={theme.textMuted}>
              ↳ Loaded {pathFormatter.format(filepath)}
            </text>
          </box>
        )}
      </For>
    </>
  )
}

function Grep(props: ToolProps) {
  const pathFormatter = usePathFormatter()
  return (
    <InlineTool icon="✱" pending="Searching content..." complete={stringValue(props.input.pattern)} part={props.part}>
      Grep "{stringValue(props.input.pattern)}"{" "}
      <Show when={stringValue(props.input.path)}>in {pathFormatter.format(stringValue(props.input.path))} </Show>
      <Show when={numberValue(props.metadata.matches)}>
        ({numberValue(props.metadata.matches)} {numberValue(props.metadata.matches) === 1 ? "match" : "matches"})
      </Show>
    </InlineTool>
  )
}

function WebFetch(props: ToolProps) {
  return (
    <InlineTool icon="%" pending="Fetching from the web..." complete={stringValue(props.input.url)} part={props.part}>
      WebFetch {stringValue(props.input.url)}
    </InlineTool>
  )
}

function WebSearch(props: ToolProps) {
  return (
    <InlineTool icon="◈" pending="Searching web..." complete={stringValue(props.input.query)} part={props.part}>
      {webSearchProviderLabel(props.metadata.provider)} "{stringValue(props.input.query)}"{" "}
      <Show when={numberValue(props.metadata.numResults)}>({numberValue(props.metadata.numResults)} results)</Show>
    </InlineTool>
  )
}

function Task(props: ToolProps) {
  const { theme } = useTheme()
  const { navigate } = useRoute()
  const sync = useSync()
  const dialog = useDialog()

  onMount(() => {
    const sessionID = stringValue(props.metadata.sessionId)
    if (sessionID && !sync.data.message[sessionID]?.length) void sync.session.sync(sessionID)
  })

  const sessionID = createMemo(() => stringValue(props.metadata.sessionId))
  const messages = createMemo(() => sync.data.message[sessionID() ?? ""] ?? [])

  const tools = createMemo(() => {
    return messages().flatMap((msg) =>
      (sync.data.part[msg.id] ?? [])
        .filter((part): part is ToolPart => part.type === "tool")
        .map((part) => ({ tool: part.tool, state: part.state })),
    )
  })

  const current = createMemo(() =>
    tools().findLast((x) => (x.state.status === "running" || x.state.status === "completed") && x.state.title),
  )

  const status = createMemo(() => sync.data.session_status[sessionID() ?? ""])
  const isRunning = createMemo(() => {
    const value = status()
    return (
      props.part.state.status === "running" ||
      (props.metadata.background === true && value !== undefined && value.type !== "idle")
    )
  })
  const retry = createMemo(() => {
    const value = status()
    if (value?.type !== "retry") return
    return value
  })

  const [now, setNow] = createSignal(Date.now())
  createEffect(() => {
    if (!isRunning()) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(id))
  })

  const duration = createMemo(() => {
    const first = messages().find((x) => x.role === "user")?.time.created
    if (!first) return 0
    const assistant = messages().findLast((x) => x.role === "assistant")?.time.completed
    const end = assistant ?? (isRunning() ? now() : 0)
    if (!end) return 0
    return Math.max(0, end - first)
  })

  const content = createMemo(() => {
    const description = stringValue(props.input.description)
    if (!description) return ""
    let content = [
      formatSubagentTitle(
        Locale.titlecase(stringValue(props.input.subagent_type) ?? "General"),
        description,
        props.metadata.background === true,
      ),
    ]

    const retrying = retry()
    if (isRunning() && retrying) {
      content.push(`↳ ${formatSubagentRetry(retrying.attempt, Locale.truncate(retrying.message, 80))}`)
    } else if (isRunning() && tools().length > 0) {
      if (current()) {
        const state = current()!.state
        const title = state.status === "running" || state.status === "completed" ? state.title : undefined
        content.push(`↳ ${Locale.titlecase(current()!.tool)} ${title}`)
      } else content.push(`↳ ${formatSubagentToolcalls(tools().length)}`)
    }

    if (isRunning() && duration() > 0) {
      content.push(`↳ ${Locale.duration(duration())}`)
    } else if (!isRunning() && props.part.state.status === "completed") {
      content.push(`↳ ${formatCompletedSubagentDetail(tools().length, Locale.duration(duration()))}`)
    }

    return content.join("\n")
  })

  return (
    <InlineTool
      icon={props.part.state.status === "completed" ? "✓" : "│"}
      separate={true}
      color={retry() ? theme.error : undefined}
      spinner={isRunning()}
      complete={stringValue(props.input.description)}
      pending="Delegating..."
      part={props.part}
      onClick={() => {
        if (sessionID()) {
          navigate({ type: "session", sessionID: sessionID()! })
        }
        const status = retry()
        if (status) void DialogAlert.show(dialog, "Retry Error", status.message)
      }}
    >
      {content()}
    </InlineTool>
  )
}

export function formatSubagentToolcalls(count: number) {
  return `${count} toolcall${count === 1 ? "" : "s"}`
}

export function formatSubagentTitle(agent: string, description: string, background: boolean) {
  return `${agent} Task${background ? " (background)" : ""} — ${description}`
}

export function formatSubagentRetry(attempt: number, message: string) {
  return `Retrying (attempt ${attempt}) · ${message}`
}

export function formatCompletedSubagentDetail(toolcalls: number, duration: string) {
  if (toolcalls === 0) return duration
  return `${formatSubagentToolcalls(toolcalls)} · ${duration}`
}

type ExecuteCall = { tool: string; status: "running" | "completed" | "error"; input?: Record<string, unknown> }

function executeCalls(value: unknown): ExecuteCall[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((call) => {
    const item = recordValue(call)
    const tool = stringValue(item?.tool)
    const status = stringValue(item?.status)
    if (!tool || !status || !["running", "completed", "error"].includes(status)) return []
    return [{ tool, status: status as ExecuteCall["status"], input: recordValue(item?.input) }]
  })
}

// The `execute` tool streams child tool calls through metadata, not a child session like Task.
function Execute(props: ToolProps) {
  const ctx = use()
  const { theme } = useTheme()
  const isLoading = createMemo(() => props.part.state.status === "pending" || props.part.state.status === "running")
  const calls = createMemo(() => executeCalls(props.metadata.toolCalls))
  const output = createMemo(() => stripAnsi(props.output?.trim() ?? ""))
  const hasRuntimeError = createMemo(() => props.metadata.error === true)
  const outputPreview = createMemo(() => collapseToolOutput(output(), 4, 4 * Math.max(20, ctx.width - 6)).output)
  const showOutput = createMemo(() => output() && hasRuntimeError())
  const content = createMemo(() => {
    const lines = ["execute"]
    for (const call of calls()) {
      const args = input(call.input ?? {})
      lines.push(`↳ ${call.tool}${args ? ` ${args}` : ""}${call.status === "error" ? " (failed)" : ""}`)
    }
    return lines.join("\n")
  })

  return (
    <>
      <InlineTool
        icon={hasRuntimeError() ? "✗" : props.part.state.status === "completed" ? "✓" : "│"}
        color={hasRuntimeError() ? theme.error : undefined}
        spinner={isLoading()}
        pending="execute"
        complete={true}
        part={props.part}
      >
        {content()}
      </InlineTool>
      <Show when={showOutput()}>
        <box paddingLeft={3}>
          <For each={outputPreview().split("\n")}>
            {(line, index) => (
              <text paddingLeft={3} fg={theme.error}>
                {index() === 0 ? "↳ " : "  "}
                {line}
              </text>
            )}
          </For>
        </box>
      </Show>
    </>
  )
}

function Edit(props: ToolProps) {
  const ctx = use()
  const { theme, syntax } = useTheme()
  const pathFormatter = usePathFormatter()

  const view = createMemo(() => {
    const diffStyle = ctx.tui.diff_style
    if (diffStyle === "stacked") return "unified"
    // Default to "auto" behavior
    return ctx.width > 120 ? "split" : "unified"
  })

  const ft = createMemo(() => filetype(stringValue(props.input.filePath)))

  const diffContent = createMemo(() => stringValue(props.metadata.diff) ?? "")

  return (
    <Switch>
      <Match when={stringValue(props.metadata.diff) !== undefined}>
        <BlockTool title={"← Edit " + pathFormatter.format(stringValue(props.input.filePath))} part={props.part}>
          <box paddingLeft={1}>
            <diff
              diff={diffContent()}
              view={view()}
              filetype={ft()}
              syntaxStyle={syntax()}
              showLineNumbers={true}
              width="100%"
              wrapMode={ctx.diffWrapMode()}
              fg={theme.text}
              addedBg={theme.diffAddedBg}
              removedBg={theme.diffRemovedBg}
              contextBg={theme.diffContextBg}
              addedSignColor={theme.diffHighlightAdded}
              removedSignColor={theme.diffHighlightRemoved}
              lineNumberFg={theme.diffLineNumber}
              lineNumberBg={theme.diffContextBg}
              addedLineNumberBg={theme.diffAddedLineNumberBg}
              removedLineNumberBg={theme.diffRemovedLineNumberBg}
            />
          </box>
          <Diagnostics diagnostics={props.metadata.diagnostics} filePath={stringValue(props.input.filePath) ?? ""} />
        </BlockTool>
      </Match>
      <Match when={true}>
        <InlineTool icon="←" pending="Preparing edit..." complete={stringValue(props.input.filePath)} part={props.part}>
          Edit {pathFormatter.format(stringValue(props.input.filePath))} {input({ replaceAll: props.input.replaceAll })}
        </InlineTool>
      </Match>
    </Switch>
  )
}

function ApplyPatch(props: ToolProps) {
  const ctx = use()
  const { theme, syntax } = useTheme()
  const pathFormatter = usePathFormatter()

  const files = createMemo(() => parseApplyPatchFiles(props.metadata.files))

  const view = createMemo(() => {
    const diffStyle = ctx.tui.diff_style
    if (diffStyle === "stacked") return "unified"
    return ctx.width > 120 ? "split" : "unified"
  })

  function Diff(p: { diff: string; filePath: string }) {
    return (
      <box paddingLeft={1}>
        <diff
          diff={p.diff}
          view={view()}
          filetype={filetype(p.filePath)}
          syntaxStyle={syntax()}
          showLineNumbers={true}
          width="100%"
          wrapMode={ctx.diffWrapMode()}
          fg={theme.text}
          addedBg={theme.diffAddedBg}
          removedBg={theme.diffRemovedBg}
          contextBg={theme.diffContextBg}
          addedSignColor={theme.diffHighlightAdded}
          removedSignColor={theme.diffHighlightRemoved}
          lineNumberFg={theme.diffLineNumber}
          lineNumberBg={theme.diffContextBg}
          addedLineNumberBg={theme.diffAddedLineNumberBg}
          removedLineNumberBg={theme.diffRemovedLineNumberBg}
        />
      </box>
    )
  }

  function title(file: { type: string; relativePath: string; filePath: string; deletions: number }) {
    if (file.type === "delete") return "# Deleted " + file.relativePath
    if (file.type === "add") return "# Created " + file.relativePath
    if (file.type === "move") return "# Moved " + pathFormatter.format(file.filePath) + " → " + file.relativePath
    return "← Patched " + file.relativePath
  }

  return (
    <Switch>
      <Match when={files().length > 0}>
        <For each={files()}>
          {(file) => (
            <BlockTool title={title(file)} part={props.part}>
              <Show
                when={file.type !== "delete"}
                fallback={
                  <text fg={theme.diffRemoved}>
                    -{file.deletions} line{file.deletions !== 1 ? "s" : ""}
                  </text>
                }
              >
                <Diff diff={file.patch} filePath={file.filePath} />
                <Diagnostics diagnostics={props.metadata.diagnostics} filePath={file.movePath ?? file.filePath} />
              </Show>
            </BlockTool>
          )}
        </For>
      </Match>
      <Match when={true}>
        <InlineTool icon="%" pending="Preparing patch..." failure="Patch failed" complete={false} part={props.part}>
          Patch
        </InlineTool>
      </Match>
    </Switch>
  )
}

function TodoWrite(props: ToolProps) {
  const todos = createMemo(() => parseTodos(props.input.todos))
  return (
    <Switch>
      <Match when={parseTodos(props.metadata.todos).length}>
        <BlockTool title="# Todos" part={props.part}>
          <box>
            <For each={todos()}>{(todo) => <TodoItem status={todo.status} content={todo.content} />}</For>
          </box>
        </BlockTool>
      </Match>
      <Match when={true}>
        <InlineTool
          icon="⚙"
          pending="Updating todos..."
          failure="Todo update failed"
          complete={false}
          part={props.part}
        >
          Updating todos...
        </InlineTool>
      </Match>
    </Switch>
  )
}

function Question(props: ToolProps) {
  const { theme } = useTheme()
  const questions = createMemo(() => parseQuestions(props.input.questions))
  const answers = createMemo(() => parseQuestionAnswers(props.metadata.answers))
  const count = createMemo(() => questions().length)

  function format(answer?: ReadonlyArray<string>) {
    if (!answer?.length) return "(no answer)"
    return answer.join(", ")
  }

  return (
    <Switch>
      <Match when={answers()}>
        <BlockTool title="# Questions" part={props.part}>
          <box gap={1}>
            <For each={questions()}>
              {(q, i) => (
                <box flexDirection="column">
                  <text fg={theme.textMuted}>{q.question}</text>
                  <text fg={theme.text}>{format(answers()?.[i()])}</text>
                </box>
              )}
            </For>
          </box>
        </BlockTool>
      </Match>
      <Match when={true}>
        <InlineTool icon="→" pending="Asking questions..." complete={count()} part={props.part}>
          Asked {count()} question{count() !== 1 ? "s" : ""}
        </InlineTool>
      </Match>
    </Switch>
  )
}

function Skill(props: ToolProps) {
  return (
    <InlineTool icon="→" pending="Loading skill..." complete={stringValue(props.input.name)} part={props.part}>
      Skill "{stringValue(props.input.name)}"
    </InlineTool>
  )
}

function Diagnostics(props: { diagnostics: unknown; filePath: string }) {
  const { theme } = useTheme()
  const terminalEnvironment = useTuiTerminalEnvironment()
  const errors = createMemo(() => {
    const normalized = normalizePath(
      typeof props.filePath === "string" ? props.filePath : "",
      terminalEnvironment.platform,
    )
    return parseDiagnostics(props.diagnostics, normalized)
  })

  return (
    <Show when={errors().length}>
      <box>
        <For each={errors()}>
          {(diagnostic) => (
            <text fg={theme.error}>
              Error [{diagnostic.range.start.line + 1}:{diagnostic.range.start.character + 1}] {diagnostic.message}
            </text>
          )}
        </For>
      </box>
    </Show>
  )
}

function input(input: Record<string, unknown>, omit?: string[]): string {
  const primitives = Object.entries(input).filter(([key, value]) => {
    if (omit?.includes(key)) return false
    return typeof value === "string" || typeof value === "number" || typeof value === "boolean"
  })
  if (primitives.length === 0) return ""
  return `[${primitives.map(([key, value]) => `${key}=${value}`).join(", ")}]`
}

function stringValue(value: unknown) {
  return typeof value === "string" ? value : undefined
}

function numberValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

const toolDisplays = new Set([
  "bash",
  "glob",
  "read",
  "grep",
  "webfetch",
  "websearch",
  "write",
  "edit",
  "task",
  "apply_patch",
  "todowrite",
  "question",
  "skill",
  "execute",
])

export function toolDisplay(tool: string) {
  if (tool === "artemis_swarm" || tool === "artemis_ask_flags") return "artemis_swarm"
  return toolDisplays.has(tool) ? tool : "generic"
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return
  return value as Record<string, unknown>
}

export function parseApplyPatchFiles(value: unknown) {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const file = recordValue(item)
    if (!file) return []
    const type = stringValue(file.type)
    const relativePath = stringValue(file.relativePath)
    const filePath = stringValue(file.filePath)
    const patch = stringValue(file.patch)
    const deletions = numberValue(file.deletions)
    if (!type || !relativePath || !filePath || patch === undefined || deletions === undefined) return []
    return [{ type, relativePath, filePath, patch, deletions, movePath: stringValue(file.movePath) }]
  })
}

export function parseTodos(value: unknown) {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const todo = recordValue(item)
    const status = stringValue(todo?.status)
    const content = stringValue(todo?.content)
    return status && content ? [{ status, content }] : []
  })
}

export function parseQuestions(value: unknown) {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const question = stringValue(recordValue(item)?.question)
    return question ? [{ question }] : []
  })
}

export function parseQuestionAnswers(value: unknown) {
  if (!Array.isArray(value)) return
  return value.map((answer) =>
    Array.isArray(answer) ? answer.filter((item): item is string => typeof item === "string") : [],
  )
}

export function parseDiagnostics(value: unknown, filePath: string) {
  const diagnostics = recordValue(value)?.[filePath]
  if (!Array.isArray(diagnostics)) return []
  return diagnostics
    .flatMap((item) => {
      const diagnostic = recordValue(item)
      const start = recordValue(recordValue(diagnostic?.range)?.start)
      const line = numberValue(start?.line)
      const character = numberValue(start?.character)
      const message = stringValue(diagnostic?.message)
      if (diagnostic?.severity !== 1 || line === undefined || character === undefined || !message) return []
      return [{ range: { start: { line, character } }, message }]
    })
    .slice(0, 3)
}
