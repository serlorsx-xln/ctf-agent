import { createMemo, createSignal } from "solid-js"
import { useLocal } from "../context/local"
import { map, pipe, flatMap, entries, filter, sortBy, take } from "remeda"
import { DialogSelect } from "../ui/dialog-select"
import { useDialog, type DialogContext } from "../ui/dialog"
import { createDialogProviderOptions, DialogProvider } from "./dialog-provider"
import { DialogVariant } from "./dialog-variant"
import * as fuzzysort from "fuzzysort"
import { useConnected } from "./use-connected"
import { useSync } from "../context/sync"
import { useTheme } from "../context/theme"
import {
  ARTEMIS_CHAT_PROVIDERS,
  artemisModelFooter,
  artemisProviderName,
  fromRaceSpec,
  isArtemisChatModel,
  toRaceSpec,
  typedConnectModelOptions,
} from "../util/artemis-models"
import { daemon } from "../artemis/client"
import { useBindings } from "../keymap"
import { confirmCancelSetup } from "./dialog-confirm-restart"

export function sortModelOptions<T extends { footer?: string; releaseDate: string | number; title: string }>(
  options: T[],
  newestFirst: boolean,
) {
  if (newestFirst) return sortBy(options, [(option) => option.releaseDate, "desc"], (option) => option.title)
  return sortBy(
    options,
    (option) => option.footer !== "Free",
    [(option) => option.releaseDate, "desc"],
    (option) => option.title,
  )
}

function PickOneModel(props: { preselected?: string; onPick: (spec: string | null) => void }) {
  const dialog = useDialog()

  async function cancel() {
    const ok = await confirmCancelSetup(dialog)
    if (!ok) return
    props.onPick(null)
    dialog.clear()
  }

  useBindings(() => ({
    priority: 1,
    enabled: dialog.stack.length <= 1,
    bindings: [
      {
        key: "escape",
        desc: "Confirm cancel",
        group: "Dialog",
        cmd: () => void cancel(),
      },
    ],
  }))

  return (
    <DialogModel
      providerID={undefined}
      pickOne={props.onPick}
      preselectedSpec={props.preselected}
      onEscape={() => void cancel()}
    />
  )
}

export function DialogModel(props: {
  providerID?: string
  pickOne?: (spec: string | null) => void
  preselectedSpec?: string
  onEscape?: () => void
}) {
  const local = useLocal()
  const sync = useSync()
  const dialog = useDialog()
  const { theme } = useTheme()
  const [query, setQuery] = createSignal("")

  const connected = useConnected()
  const providers = createDialogProviderOptions()
  const connectedProviders = createMemo(() => new Set(sync.data.provider_next?.connected ?? []))

  const showExtra = createMemo(() => connected() && !props.providerID)

  const options = createMemo(() => {
    const needle = query().trim()
    const showSections = showExtra() && needle.length === 0
    const favorites = connected() ? local.model.favorite() : []
    const recents = local.model.recent()
    const authOnly = process.env.ARTEMIS === "1" ? connectedProviders() : null

    function isAuthed(providerID: string): boolean {
      if (!authOnly) return true
      return authOnly.has(providerID)
    }

    function modalitiesOf(_model: unknown): { input?: string[]; output?: string[] } | undefined {
      const m = _model as { modalities?: { input?: string[]; output?: string[] } }
      return m.modalities
    }

    function toOptions(items: typeof favorites, category: string) {
      if (!showSections) return []
      return items.flatMap((item) => {
        const provider = sync.data.provider.find((provider) => provider.id === item.providerID)
        if (!provider) return []
        if (!isAuthed(provider.id)) return []
        const model = provider.models[item.modelID]
        if (!model) return []
        if (
          !isArtemisChatModel({
            providerID: provider.id,
            modelID: model.id,
            name: model.name,
            modalities: modalitiesOf(model),
          })
        )
          return []
        return [
          {
            key: item,
            value: { providerID: provider.id, modelID: model.id },
            title: model.name ?? item.modelID,
            description: provider.name,
            category,
            disabled: false,
            footer: artemisModelFooter(provider.id),
            onSelect: () => {
              onSelect(provider.id, model.id)
            },
          },
        ]
      })
    }

    const favoriteOptions = toOptions(favorites, "Favorites")
    const recentOptions = toOptions(
      recents.filter(
        (item) => !favorites.some((fav) => fav.providerID === item.providerID && fav.modelID === item.modelID),
      ),
      "Recent",
    )

    const providerOptions = pipe(
      sync.data.provider,
      filter((provider) => process.env.ARTEMIS !== "1" || ARTEMIS_CHAT_PROVIDERS.has(provider.id)),
      filter((provider) => isAuthed(provider.id)),
      sortBy((provider) => provider.name),
      flatMap((provider) =>
        pipe(
          provider.models,
          entries(),
          filter(([_, info]) => info.status !== "deprecated"),
          filter(([_, info]) => (props.providerID ? info.providerID === props.providerID : true)),
          filter(([model, info]) =>
            isArtemisChatModel({
              providerID: provider.id,
              modelID: model,
              name: info.name,
              modalities: modalitiesOf(info),
            }),
          ),
          map(([model, info]) => ({
            value: { providerID: provider.id, modelID: model },
            title: info.name ?? model,
            releaseDate: info.release_date,
            description: favorites.some((item) => item.providerID === provider.id && item.modelID === model)
              ? "(Favorite)"
              : undefined,
            category: connected()
              ? process.env.ARTEMIS === "1"
                ? artemisProviderName(provider.id, provider.name)
                : provider.name
              : undefined,
            disabled: false,
            footer: artemisModelFooter(provider.id),
            onSelect() {
              onSelect(provider.id, model)
            },
          })),
          filter((option) => {
            if (!showSections) return true
            if (
              favorites.some(
                (item) => item.providerID === option.value.providerID && item.modelID === option.value.modelID,
              )
            )
              return false
            if (
              recents.some(
                (item) => item.providerID === option.value.providerID && item.modelID === option.value.modelID,
              )
            )
              return false
            return true
          }),
          (options) => sortModelOptions(options, props.providerID !== undefined),
        ),
      ),
    )

    // Artemis solve picker: only authenticated providers (never "popular" unauthed).
    const popularProviders =
      process.env.ARTEMIS === "1" || connected()
        ? []
        : pipe(
            providers(),
            map((option) => ({
              ...option,
              category: "Popular providers",
            })),
            take(6),
          )

    if (needle) {
      const filtered = sortModelOptions(
        fuzzysort.go(needle, providerOptions, { keys: ["title", "category"] }).map((x) => x.obj),
        false,
      )
      const typed =
        process.env.ARTEMIS === "1"
          ? typedConnectModelOptions(
              needle,
              [...connectedProviders()],
              filtered.map((opt) => opt.value),
              props.providerID,
            ).map((value) => ({
              value,
              title: value.modelID,
              description: "Use this id on Claude",
              category: "Custom",
              disabled: false,
              footer: artemisModelFooter(value.providerID),
              onSelect() {
                onSelect(value.providerID, value.modelID)
              },
            }))
          : []
      return [
        ...typed,
        ...filtered,
        ...fuzzysort.go(needle, popularProviders, { keys: ["title"] }).map((x) => x.obj),
      ]
    }

    return [...favoriteOptions, ...recentOptions, ...providerOptions, ...popularProviders]
  })

  const provider = createMemo(() =>
    props.providerID ? sync.data.provider.find((item) => item.id === props.providerID) : null,
  )

  const title = createMemo(() => {
    const value = provider()
    const authed = process.env.ARTEMIS === "1" ? connectedProviders().size > 0 : true
    if (process.env.ARTEMIS === "1" && props.pickOne && !authed) {
      return "Select model — /connect required"
    }
    const base = value ? value.name : props.pickOne ? "Select model" : "Select model"
    const last = daemon.lastModels[0]()
    if (process.env.ARTEMIS === "1" && last.length) {
      return `${base} · last: ${last.join(", ")}`
    }
    return base
  })

  function onSelect(providerID: string, modelID: string) {
    if (props.pickOne) {
      props.pickOne(toRaceSpec(providerID, modelID))
      dialog.clear()
      return
    }
    local.model.set({ providerID, modelID }, { recent: true })
    const list = local.model.variant.list()
    const cur = local.model.variant.selected()
    if (cur === "default" || (cur && list.includes(cur))) {
      dialog.clear()
      return
    }
    if (list.length > 0) {
      dialog.replace(() => <DialogVariant />)
      return
    }
    dialog.clear()
  }

  const current = createMemo(() => {
    if (props.preselectedSpec) {
      const parsed = fromRaceSpec(props.preselectedSpec)
      if (parsed) return parsed
    }
    return local.model.current()
  })

  return (
    <DialogSelect<ReturnType<typeof options>[number]["value"]>
      options={options()}
      actions={
        props.pickOne
          ? []
          : [
              {
                command: "model.dialog.provider",
                title: connected() ? "Connect provider" : "View all providers",
                onTrigger() {
                  dialog.replace(() => <DialogProvider />)
                },
              },
              {
                command: "model.dialog.favorite",
                title: "Favorite",
                hidden: !connected(),
                onTrigger: (option) => {
                  local.model.toggleFavorite(option.value as { providerID: string; modelID: string })
                },
              },
            ]
      }
      emptyView={
        process.env.ARTEMIS === "1" && connectedProviders().size === 0 ? (
          <box paddingTop={1} paddingBottom={1}>
            <text fg={theme.textMuted}>No authenticated models — run /connect first (Cursor / Claude / Codex / Gemini)</text>
          </box>
        ) : undefined
      }
      onFilter={setQuery}
      flat={true}
      skipFilter={true}
      title={title()}
      current={current()}
      onEscape={props.onEscape}
      onSelect={(option) => {
        const value = option.value
        if (typeof value !== "object" || value === null || !("providerID" in value)) return
        onSelect(value.providerID, value.modelID)
      }}
    />
  )
}

DialogModel.showPickOne = (dialog: DialogContext, options?: { preselected?: string }) => {
  return new Promise<string | null>((resolve) => {
    dialog.replace(
      () => <PickOneModel preselected={options?.preselected} onPick={(spec) => resolve(spec)} />,
      () => resolve(null),
    )
  })
}

/** Prefill local.model from a race spec when safe for the Artemis orchestrator.

 * Swarm models (Claude / Codex / Gemini / ChatGPT) must NOT replace the chat model —
 * Artemis load/ask_flags goes through the Cursor stub. Prefilling openai/gpt-5.4
 * after a swarm pick caused ChatGPT-quota errors and wiped the solve UI.
 */
export function prefillModelFromRaceSpec(spec: string | undefined, local: ReturnType<typeof useLocal>): void {
  if (!spec) return
  const parsed = fromRaceSpec(spec)
  if (!parsed) return
  if (process.env.ARTEMIS === "1" && parsed.providerID !== "cursor") return
  local.model.set(parsed, { recent: false })
}
