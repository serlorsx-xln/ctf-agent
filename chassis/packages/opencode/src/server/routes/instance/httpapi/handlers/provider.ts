import { ProviderAuth } from "@/provider/auth"
import { Config } from "@/config/config"
import { ModelsDev } from "@opencode-ai/core/models-dev"
import { Provider } from "@/provider/provider"
import { Auth } from "@/auth"
import { artemisConnectedIds } from "@/provider/artemis-auth"

import { mapValues } from "remeda"
import { Effect, Schema } from "effect"
import { HttpServerRequest, HttpServerResponse } from "effect/unstable/http"
import { HttpApiBuilder } from "effect/unstable/httpapi"
import { InstanceHttpApi } from "../api"
import { ProviderAuthApiError } from "../groups/provider"
import { ProviderV2 } from "@opencode-ai/core/provider"

function mapProviderAuthError<A, R>(self: Effect.Effect<A, ProviderAuth.Error, R>) {
  return self.pipe(
    Effect.mapError((error) => {
      if (error instanceof ProviderAuth.OauthMissing) {
        return new ProviderAuthApiError({ name: error._tag, data: { providerID: error.providerID } })
      }
      if (error instanceof ProviderAuth.OauthCodeMissing) {
        return new ProviderAuthApiError({ name: error._tag, data: { providerID: error.providerID } })
      }
      if (error instanceof ProviderAuth.OauthCallbackFailed) {
        return new ProviderAuthApiError({ name: error._tag, data: {} })
      }
      if (error instanceof ProviderAuth.ValidationFailed) {
        return new ProviderAuthApiError({ name: error._tag, data: { field: error.field, message: error.message } })
      }
      return new ProviderAuthApiError({ name: "BadRequest", data: {} })
    }),
  )
}

export const providerHandlers = HttpApiBuilder.group(InstanceHttpApi, "provider", (handlers) =>
  Effect.gen(function* () {
    const cfg = yield* Config.Service
    const provider = yield* Provider.Service
    const svc = yield* ProviderAuth.Service

    const authStore = yield* Auth.Service

    const list = Effect.fn("ProviderHttpApi.list")(function* () {
      const config = yield* cfg.get()
      const all = yield* ModelsDev.Service.use((s) => s.get())
      const disabled = new Set(config.disabled_providers ?? [])
      const enabled = config.enabled_providers ? new Set(config.enabled_providers) : undefined
      const filtered: Record<string, (typeof all)[string]> = {}
      for (const [key, value] of Object.entries(all)) {
        if ((enabled ? enabled.has(key) : true) && !disabled.has(key)) filtered[key] = value
      }
      const loaded = yield* provider.list()
      const auths = yield* authStore.all().pipe(Effect.orDie)
      // Artemis: ✓ only when credentials exist (config rename alone is not connected)
      const connected = artemisConnectedIds(loaded, auths)
      const providers = Object.assign(
        mapValues(filtered, (item) => Provider.fromModelsDevProvider(item)),
        loaded,
      )

      // Artemis: Cursor is a custom provider — keep it visible in /connect even when not auth'd
      if (process.env.ARTEMIS === "1" && (!enabled || enabled.has("cursor")) && !providers["cursor"]) {
        const cfgCursor = config.provider?.cursor
        providers["cursor"] = {
          id: ProviderV2.ID.make("cursor"),
          name: cfgCursor?.name ?? "Cursor",
          source: "config",
          env: cfgCursor?.env ?? ["CURSOR_API_KEY"],
          options: cfgCursor?.options ?? {},
          models: {
            default: {
              id: "default" as never,
              name: "Auto",
              providerID: ProviderV2.ID.make("cursor"),
              api: {
                id: "default",
                npm: "@ai-sdk/openai-compatible",
                url: "http://127.0.0.1:18765/v1",
              },
              status: "active",
              headers: {},
              options: {},
              cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
              limit: { context: 200_000, output: 64_000 },
              capabilities: {
                temperature: true,
                reasoning: false,
                attachment: false,
                toolcall: true,
                input: { text: true, audio: false, image: false, video: false, pdf: false },
                output: { text: true, audio: false, image: false, video: false, pdf: false },
                interleaved: false,
              },
              release_date: "",
              variants: {},
              family: "",
            },
          },
        } as (typeof providers)[string]
      }

      // Artemis display names
      if (process.env.ARTEMIS === "1") {
        if (providers["anthropic"]) providers["anthropic"] = { ...providers["anthropic"], name: "Claude" }
        if (providers["openai"]) providers["openai"] = { ...providers["openai"], name: "Codex" }
      }

      return {
        all: Object.values(providers).map(Provider.toPublicInfo),
        default: Provider.defaultModelIDs(providers),
        connected,
      }
    })

    const auth = Effect.fn("ProviderHttpApi.auth")(function* () {
      return yield* svc.methods()
    })

    const authorize = Effect.fn("ProviderHttpApi.authorize")(function* (ctx: {
      params: { providerID: ProviderV2.ID }
      payload: ProviderAuth.AuthorizeInput
    }) {
      return yield* mapProviderAuthError(
        svc.authorize({
          providerID: ctx.params.providerID,
          method: ctx.payload.method,
          inputs: ctx.payload.inputs,
        }),
      )
    })

    const authorizeRaw = Effect.fn("ProviderHttpApi.authorizeRaw")(function* (ctx: {
      params: { providerID: ProviderV2.ID }
      request: HttpServerRequest.HttpServerRequest
    }) {
      const body = yield* Effect.orDie(ctx.request.text)
      const payload = yield* Schema.decodeUnknownEffect(Schema.fromJsonString(ProviderAuth.AuthorizeInput))(body).pipe(
        Effect.mapError(() => new ProviderAuthApiError({ name: "BadRequest", data: {} })),
      )
      // Match legacy route behavior: when authorize() resolves without a
      // result (e.g. no further redirect), serialize as JSON `null` instead
      // of an empty body so clients can `.json()` parse the response.
      const result = yield* authorize({ params: ctx.params, payload })
      return HttpServerResponse.jsonUnsafe(result ?? null)
    })

    const callback = Effect.fn("ProviderHttpApi.callback")(function* (ctx: {
      params: { providerID: ProviderV2.ID }
      payload: ProviderAuth.CallbackInput
    }) {
      yield* mapProviderAuthError(
        svc.callback({
          providerID: ctx.params.providerID,
          method: ctx.payload.method,
          code: ctx.payload.code,
        }),
      )
      return true
    })

    return handlers
      .handle("list", list)
      .handle("auth", auth)
      .handleRaw("authorize", authorizeRaw)
      .handle("callback", callback)
  }),
)
