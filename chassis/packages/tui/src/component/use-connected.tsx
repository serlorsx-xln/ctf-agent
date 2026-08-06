import { createMemo } from "solid-js"
import { useSync } from "../context/sync"

export function useConnected() {
  const sync = useSync()
  return createMemo(() => {
    // Artemis: ✓ / onboarded only when provider_next.connected has real credentials
    if (process.env.ARTEMIS === "1") {
      return (sync.data.provider_next?.connected?.length ?? 0) > 0
    }
    return sync.data.provider.some(
      (provider) =>
        provider.id !== "opencode" || Object.values(provider.models).some((model) => model.cost?.input !== 0),
    )
  })
}
