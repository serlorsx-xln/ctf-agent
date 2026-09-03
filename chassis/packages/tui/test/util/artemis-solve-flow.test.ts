import { describe, expect, test } from "bun:test"
import type { DialogContext } from "../../src/ui/dialog"
import { DialogFlagsRequired } from "../../src/component/dialog-flags-required"
import { DialogSolveMode } from "../../src/component/dialog-solve-mode"
import { DialogModel } from "../../src/component/dialog-model"
import { DialogModelsSwarm } from "../../src/component/dialog-models-swarm"
import { runSolveFlowGate } from "../../src/util/artemis-solve-flow"

const dialog = {} as DialogContext

describe("runSolveFlowGate", () => {
  test("cancel at flags returns null", async () => {
    const orig = DialogFlagsRequired.show
    DialogFlagsRequired.show = async () => null
    try {
      expect(await runSolveFlowGate(dialog)).toBeNull()
    } finally {
      DialogFlagsRequired.show = orig
    }
  })

  test("cancel at mode returns null", async () => {
    const origFlags = DialogFlagsRequired.show
    const origMode = DialogSolveMode.show
    DialogFlagsRequired.show = async () => 2
    DialogSolveMode.show = async () => null
    try {
      expect(await runSolveFlowGate(dialog)).toBeNull()
    } finally {
      DialogFlagsRequired.show = origFlags
      DialogSolveMode.show = origMode
    }
  })

  test("single path requires a model", async () => {
    const origFlags = DialogFlagsRequired.show
    const origMode = DialogSolveMode.show
    const origModel = DialogModel.showPickOne
    DialogFlagsRequired.show = async () => 1
    DialogSolveMode.show = async () => "single"
    DialogModel.showPickOne = async () => null
    try {
      expect(await runSolveFlowGate(dialog)).toBeNull()
    } finally {
      DialogFlagsRequired.show = origFlags
      DialogSolveMode.show = origMode
      DialogModel.showPickOne = origModel
    }
  })

  test("single path returns one model", async () => {
    const origFlags = DialogFlagsRequired.show
    const origMode = DialogSolveMode.show
    const origModel = DialogModel.showPickOne
    DialogFlagsRequired.show = async () => 3
    DialogSolveMode.show = async () => "single"
    DialogModel.showPickOne = async () => "claude/opus"
    try {
      expect(await runSolveFlowGate(dialog, { challengeName: "pwn" })).toEqual({
        flags: 3,
        mode: "single",
        models: ["claude/opus"],
      })
    } finally {
      DialogFlagsRequired.show = origFlags
      DialogSolveMode.show = origMode
      DialogModel.showPickOne = origModel
    }
  })

  test("swarm path requires models", async () => {
    const origFlags = DialogFlagsRequired.show
    const origMode = DialogSolveMode.show
    const origSwarm = DialogModelsSwarm.show
    DialogFlagsRequired.show = async () => 1
    DialogSolveMode.show = async () => "swarm"
    DialogModelsSwarm.show = async () => []
    try {
      expect(await runSolveFlowGate(dialog)).toBeNull()
    } finally {
      DialogFlagsRequired.show = origFlags
      DialogSolveMode.show = origMode
      DialogModelsSwarm.show = origSwarm
    }
  })

  test("swarm path returns selected models", async () => {
    const origFlags = DialogFlagsRequired.show
    const origMode = DialogSolveMode.show
    const origSwarm = DialogModelsSwarm.show
    DialogFlagsRequired.show = async () => 2
    DialogSolveMode.show = async () => "swarm"
    DialogModelsSwarm.show = async () => ["cursor/a", "claude/b"]
    try {
      expect(await runSolveFlowGate(dialog, { preselected: ["cursor/a"] })).toEqual({
        flags: 2,
        mode: "swarm",
        models: ["cursor/a", "claude/b"],
      })
    } finally {
      DialogFlagsRequired.show = origFlags
      DialogSolveMode.show = origMode
      DialogModelsSwarm.show = origSwarm
    }
  })
})
