import { DialogSelect } from "../ui/dialog-select"
import { useDialog, type DialogContext } from "../ui/dialog"
import { useBindings } from "../keymap"
import { confirmCancelSetup } from "./dialog-confirm-restart"

export type SolveMode = "single" | "swarm"

function SolveModePicker(props: { onPick: (mode: SolveMode) => void; onCancel: () => void }) {
  const dialog = useDialog()

  useBindings(() => ({
    priority: 1,
    enabled: dialog.stack.length <= 1,
    bindings: [
      {
        key: "escape",
        desc: "Confirm cancel",
        group: "Dialog",
        cmd: () => {
          void (async () => {
            const ok = await confirmCancelSetup(dialog)
            if (!ok) return
            props.onCancel()
            dialog.clear()
          })()
        },
      },
    ],
  }))

  return (
    <DialogSelect<SolveMode>
      title="Solve mode"
      options={[
        {
          value: "single",
          title: "Single",
          description: "One model solves the challenge",
          onSelect: () => props.onPick("single"),
        },
        {
          value: "swarm",
          title: "Swarm",
          description: "Multiple models race in parallel",
          onSelect: () => props.onPick("swarm"),
        },
      ]}
      onSelect={(option) => props.onPick(option.value)}
      onEscape={() => {
        void (async () => {
          const ok = await confirmCancelSetup(dialog)
          if (!ok) return
          props.onCancel()
          dialog.clear()
        })()
      }}
    />
  )
}

export const DialogSolveMode = {
  show(dialog: DialogContext): Promise<SolveMode | null> {
    return new Promise<SolveMode | null>((resolve) => {
      dialog.replace(
        () => (
          <SolveModePicker
            onPick={(mode) => resolve(mode)}
            onCancel={() => resolve(null)}
          />
        ),
        () => resolve(null),
      )
    })
  },
}
