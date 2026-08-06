import type { DialogContext } from "../ui/dialog"
import { DialogFlagsRequired } from "../component/dialog-flags-required"
import { DialogSolveMode, type SolveMode } from "../component/dialog-solve-mode"
import { DialogModelsSwarm } from "../component/dialog-models-swarm"
import { DialogModel } from "../component/dialog-model"

export type SolveFlowResult = {
  flags: number
  mode: SolveMode
  models: string[]
}

export async function runSolveFlowGate(
  dialog: DialogContext,
  opts?: { challengeName?: string; preselected?: string[]; defaultFlags?: number },
): Promise<SolveFlowResult | null> {
  const flags = await DialogFlagsRequired.show(dialog, {
    defaultValue: opts?.defaultFlags ?? 1,
    challengeName: opts?.challengeName,
    cancelable: true,
  })
  if (flags == null) return null

  const mode = await DialogSolveMode.show(dialog)
  if (mode == null) return null

  let models: string[] = []
  if (mode === "single") {
    const spec = await DialogModel.showPickOne(dialog, { preselected: opts?.preselected?.[0] })
    if (!spec) return null
    models = [spec]
  } else {
    models = await DialogModelsSwarm.show(dialog, { preselected: opts?.preselected })
  }

  if (models.length === 0) return null
  return { flags, mode, models }
}
