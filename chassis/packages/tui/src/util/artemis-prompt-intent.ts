/** Classify Artemis main-prompt submits (testable; no Solid deps). */

import { extractChallengePaths, looksLikeChallengePaste } from "./artemis-challenge-paste"

export type ArtemisPromptSubmitIntent =
  | { kind: "challenge_paste" }
  | { kind: "steer_swarm"; text: string }
  | { kind: "queue_swarm"; text: string }
  | { kind: "stop_swarm" }
  | { kind: "chat_ok" }
  | { kind: "need_challenge" }
  | { kind: "wait_for_start" }

/** `/queue …` → queue until solver idle; bare `/queue` keeps queue prefer (not stop). */
export function parseOperatorDelivery(raw: string): {
  delivery: "steer" | "queue"
  text: string
  bareQueue?: boolean
} {
  const t = (raw || "").trim()
  const m = t.match(/^\/queue(?:\s+|$)([\s\S]*)$/i)
  if (m) {
    const body = (m[1] || "").trim()
    return { delivery: "queue", text: body, bareQueue: !body }
  }
  return { delivery: "steer", text: t }
}

/**
 * Mid-solve reload signal — paths / big paste only.
 * Short operator chat ("เป็นไงบ้าง", "try XOR") must NOT count as a new challenge.
 */
export function looksLikeChallengeReload(text: string): boolean {
  const t = (text || "").trim()
  if (!t) return false
  if (extractChallengePaths(t).length > 0) return true
  if (t.length >= 400 && looksLikeChallengePaste(t)) return true
  return false
}

export function classifyArtemisPromptSubmit(opts: {
  raw: string
  looksLikeChallenge: boolean
  swarmRunning: boolean
  solveLocked: boolean
  hasChallenge: boolean
  flowCompleted: boolean
  hasPriorState: boolean
}): ArtemisPromptSubmitIntent {
  const text = opts.raw.trim()

  // Mid-solve / hold first — free text steers; only clear path/big paste reloads.
  // Empty Enter releases/stops (same as Esc /stop) — does not clear + new chat.
  if (opts.swarmRunning || opts.solveLocked) {
    if (!text) return { kind: "stop_swarm" }
    if (looksLikeChallengeReload(text)) {
      return { kind: "challenge_paste" }
    }
    const { delivery, text: body, bareQueue } = parseOperatorDelivery(text)
    // Bare `/queue` must not stop the swarm — treat as queue intent (dialog fills text).
    if (!body) {
      if (bareQueue) return { kind: "queue_swarm", text: "" }
      return { kind: "stop_swarm" }
    }
    if (delivery === "queue") return { kind: "queue_swarm", text: body }
    return { kind: "steer_swarm", text: body }
  }

  if (opts.looksLikeChallenge) {
    return { kind: "challenge_paste" }
  }
  if (!opts.hasPriorState && !opts.hasChallenge) {
    return { kind: "need_challenge" }
  }
  if (opts.hasChallenge && !opts.flowCompleted) {
    return { kind: "wait_for_start" }
  }
  return { kind: "chat_ok" }
}
