import { DialogConfirm } from "../ui/dialog-confirm"
import type { DialogContext } from "../ui/dialog"

export type ConfirmRestartOpts = {
  /** Prior solve finished successfully. */
  completed?: boolean
  /** Swarm / solve still in progress — warn that work will be stopped. */
  active?: boolean
}

/** Confirm wiping prior challenge UI/chat before a new path, message, or slash. */
export const DialogConfirmRestart = {
  show(dialog: DialogContext, opts?: ConfirmRestartOpts) {
    if (opts?.active) {
      return DialogConfirm.show(
        dialog,
        "Stop current work?",
        "This will stop the challenge solve in progress (swarm, tools, and related work), then clear chat, challenge sidebar, and swarm data. Continue?",
      )
    }
    const completed = opts?.completed === true
    return DialogConfirm.show(
      dialog,
      completed ? "Start new challenge?" : "Clear and start fresh?",
      completed
        ? "The previous solve is complete. Clear chat, challenge sidebar, and swarm data before continuing?"
        : "Sending again will clear previous chat, challenge sidebar, and swarm cards so they do not stack. Continue?",
    )
  },
}

/** Confirm stopping a live swarm (Esc stop, /stop). Same wording as active restart. */
export function confirmStopWork(dialog: DialogContext) {
  return DialogConfirm.show(
    dialog,
    "Stop current work?",
    "This will stop the challenge solve in progress (swarm, tools, and related work). Continue?",
  )
}

/** Confirm discarding an in-progress solve setup dialog (flags / mode / models). */
export function confirmCancelSetup(dialog: DialogContext) {
  return DialogConfirm.show(
    dialog,
    "Cancel setup?",
    "This will discard the current solve setup and return to chat. Continue?",
  )
}
