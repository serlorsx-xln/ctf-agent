/**
 * First-message paste rules.
 *
 * Spec comes from shared/challenge_paste.json (same source as Python
 * backend.challenge_paste). path_re extracts host paths. greetings is the
 * only reject list — a fresh session treats everything else as a challenge.
 */
import patterns from "../../../../../shared/challenge_paste.json"

const PATH_RE = new RegExp(patterns.path_re, "g")
const GREETINGS = new RegExp(patterns.greetings, "i")

/** Empty or a bare hello — not a challenge. */
export function isGreeting(text: string): boolean {
  const t = (text || "").trim()
  if (!t) return true
  return GREETINGS.test(t)
}

/** True for any non-empty, non-greeting paste. */
export function looksLikeChallengePaste(text: string): boolean {
  const t = (text || "").trim()
  if (!t || isGreeting(t)) return false
  return true
}

/** Host paths mentioned in the text (folder / attachments). */
export function extractChallengePaths(text: string): string[] {
  if (!text) return []
  const seen: string[] = []
  for (const m of text.matchAll(PATH_RE)) {
    const p = m[1]!.replace(/[.,;:)"']+$/, "")
    if (!seen.includes(p)) seen.push(p)
  }
  return seen
}

/** User text with detected paths removed. */
export function extractPasteWithoutPaths(text: string, paths: string[]): string {
  if (!text) return ""
  let out = text
  for (const p of paths) out = out.replaceAll(p, " ")
  return out.trim()
}

/** Build daemon `load` args from a prompt string. */
export function loadArgsFromPrompt(text: string): Record<string, unknown> {
  const paths = extractChallengePaths(text)
  const paste = extractPasteWithoutPaths(text, paths)
  const args: Record<string, unknown> = { mode: "artemis" }
  if (paths.length > 0) {
    args.path = paths[0]
    if (paths.length > 1) args.attachments = paths.slice(1)
  }
  if (paste) args.prompt = paste
  return args
}
