import { DialogSelect } from "../ui/dialog-select"
import type { DialogContext } from "../ui/dialog"
import { createSignal } from "solid-js"

export type OperatorDeliveryChoice = "steer" | "queue"

const lastChoice = createSignal<OperatorDeliveryChoice>("steer")

/**
 * Ask how to deliver a mid-solve note.
 * Main → all agents; agent page → that agent (targetLabel).
 * ``softOnly``: Queue uses soft turn-boundary wording (Claude/Codex/Gemini).
 */
export const DialogOperatorDelivery = {
  show(
    dialog: DialogContext,
    opts?: {
      prefer?: OperatorDeliveryChoice
      targetLabel?: string
      softOnly?: boolean
    },
  ): Promise<OperatorDeliveryChoice | null> {
    const prefer = opts?.prefer ?? lastChoice[0]()
    const who = (opts?.targetLabel || "all agents").trim() || "all agents"
    const soft = Boolean(opts?.softOnly)

    return new Promise<OperatorDeliveryChoice | null>((resolve) => {
      let settled = false
      const finish = (value: OperatorDeliveryChoice | null) => {
        if (settled) return
        settled = true
        if (value) lastChoice[1](value)
        dialog.pop({ invokeClose: false })
        resolve(value)
      }

      dialog.push(
        () => (
          <DialogSelect<OperatorDeliveryChoice>
            title={`Send to ${who}?`}
            current={prefer}
            options={[
              {
                value: "steer",
                title: "Send now",
                description:
                  who === "all agents"
                    ? "Force-followup on every running agent (interrupt each turn, same session)."
                    : `Force-followup on ${who} only (interrupt that turn, same session).`,
                onSelect: () => finish("steer"),
              },
              {
                value: "queue",
                title: "Queue until idle",
                description: soft
                  ? who === "all agents"
                    ? "Does not interrupt. Each soft solver picks this up at the next turn boundary."
                    : `Does not interrupt. ${who} picks this up at the next turn boundary.`
                  : who === "all agents"
                    ? "Does not interrupt. Each agent picks this up when its turn goes idle."
                    : `Does not interrupt. ${who} picks this up when its turn goes idle.`,
                onSelect: () => finish("queue"),
              },
            ]}
            onSelect={(option) => finish(option.value)}
            onEscape={() => finish(null)}
          />
        ),
        () => finish(null),
      )
    })
  },
}
