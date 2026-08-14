import { entries, filter, flatMap, map, pipe, sortBy } from "remeda"
import type { DialogSelectOption } from "../ui/dialog-select"
import type { useLocal } from "../context/local"
import type { useSync } from "../context/sync"
import {
  ARTEMIS_CHAT_PROVIDERS,
  type ArtemisModelValue,
  artemisModelFooter,
  artemisProviderName,
  isArtemisChatModel,
  toRaceSpec,
  typedConnectModelOptions,
} from "./artemis-models"
import { sortModelOptions } from "../component/dialog-model"
import * as fuzzysort from "fuzzysort"

export type { ArtemisModelValue }

type Sync = ReturnType<typeof useSync>
type Local = ReturnType<typeof useLocal>

function modelModalities(_model: unknown): { input?: string[]; output?: string[] } | undefined {
  const m = _model as { modalities?: { input?: string[]; output?: string[] } }
  return m.modalities
}

export function buildArtemisModelOptions(input: {
  sync: Sync
  local: Local
  connected: boolean
  /** When set (Artemis), only list these authenticated provider IDs. */
  connectedProviders?: string[]
  query?: string
  providerID?: string
  preselected?: string[]
}): DialogSelectOption<ArtemisModelValue>[] {
  const needle = (input.query ?? "").trim()
  const showSections = !input.providerID && needle.length === 0
  const favorites = input.connected ? input.local.model.favorite() : []
  const recents = input.local.model.recent()
  const preselected = new Set(input.preselected ?? [])
  const authOnly =
    process.env.ARTEMIS === "1" && input.connectedProviders
      ? new Set(input.connectedProviders)
      : null

  function isAuthed(providerID: string): boolean {
    if (!authOnly) return true
    return authOnly.has(providerID)
  }

  function isPreselected(providerID: string, modelID: string): boolean {
    return preselected.has(toRaceSpec(providerID, modelID))
  }

  function toOptions(items: typeof favorites, category: string) {
    if (!showSections) return []
    return items.flatMap((item) => {
      const provider = input.sync.data.provider.find((p) => p.id === item.providerID)
      if (!provider) return []
      if (!isAuthed(provider.id)) return []
      const model = provider.models[item.modelID]
      if (!model) return []
      if (
        !isArtemisChatModel({
          providerID: provider.id,
          modelID: model.id,
          name: model.name,
          modalities: modelModalities(model),
        })
      )
        return []
      return [
        {
          value: { providerID: provider.id, modelID: model.id },
          title: model.name ?? item.modelID,
          description: provider.name,
          category,
          disabled: false,
          footer: artemisModelFooter(provider.id),
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
    input.sync.data.provider,
    filter((provider) => process.env.ARTEMIS !== "1" || ARTEMIS_CHAT_PROVIDERS.has(provider.id)),
    filter((provider) => isAuthed(provider.id)),
    sortBy((provider) => provider.name),
    flatMap((provider) =>
      pipe(
        provider.models,
        entries(),
        filter(([_, info]) => info.status !== "deprecated"),
        filter(([_, info]) => (input.providerID ? info.providerID === input.providerID : true)),
        filter(([model, info]) =>
          isArtemisChatModel({
            providerID: provider.id,
            modelID: model,
            name: info.name,
            modalities: modelModalities(info),
          }),
        ),
        map(([model, info]) => ({
          value: { providerID: provider.id, modelID: model },
          title: info.name ?? model,
          releaseDate: info.release_date,
          description: favorites.some((item) => item.providerID === provider.id && item.modelID === model)
            ? "(Favorite)"
            : isPreselected(provider.id, model)
              ? "(Selected)"
              : undefined,
          category: input.connected
            ? process.env.ARTEMIS === "1"
              ? artemisProviderName(provider.id, provider.name)
              : provider.name
            : undefined,
          disabled: false,
          footer: artemisModelFooter(provider.id),
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
        (options) => sortModelOptions(options, input.providerID !== undefined),
      ),
    ),
  )

  if (needle) {
    const filtered = sortModelOptions(
      fuzzysort.go(needle, providerOptions, { keys: ["title", "category"] }).map((x) => x.obj),
      false,
    )
    const typed = typedConnectModelOptions(
      needle,
      input.connectedProviders ?? [],
      filtered.map((opt) => opt.value),
      input.providerID,
    ).map((value) => ({
      value,
      title: value.modelID,
      description: "Use this id on Claude",
      category: "Custom",
      disabled: false,
      footer: artemisModelFooter(value.providerID),
    }))
    return [...typed, ...filtered]
  }

  return [...favoriteOptions, ...recentOptions, ...providerOptions]
}
