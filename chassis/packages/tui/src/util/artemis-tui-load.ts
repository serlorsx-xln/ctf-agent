import { daemon, type SessionState } from "../artemis/client"
import { loadArgsFromPrompt, looksLikeChallengePaste } from "./artemis-challenge-paste"

export type TuiLoadResult =
  | { status: "skip" }
  | { status: "error"; message: string }
  | { status: "ok"; text: string; session_state: SessionState }

/** Daemon load used by both the home prompt and the session interceptor. */
export async function tuiLoadChallenge(raw: string): Promise<TuiLoadResult> {
  if (!looksLikeChallengePaste(raw)) return { status: "skip" }
  try {
    const { text, session_state } = await daemon.loadChallenge(loadArgsFromPrompt(raw))
    if (/^ERROR/i.test(text.trim())) {
      return { status: "error", message: text.trim().slice(0, 160) || "Load failed" }
    }
    return { status: "ok", text, session_state }
  } catch (e) {
    return { status: "error", message: (e as Error).message || "Load failed" }
  }
}
