/**
 * Detect challenge-shaped prompt text.
 *
 * Patterns come from shared/challenge_paste.json (same source as Python
 * backend.challenge_paste) so TUI and Cursor stub cannot drift.
 */
import patterns from "../../../../../shared/challenge_paste.json"

const CHALLENGE_HINT = new RegExp(patterns.challenge_hint, "i")
const CTF_PROSE = new RegExp(patterns.ctf_prose, "i")
const PATH_RE = new RegExp(patterns.path_re, "g")
const MIN_LINES = patterns.min_lines
const MIN_CHARS = patterns.min_chars

/** True when pasted text looks like a CTF challenge description. */
export function looksLikeChallengePaste(text: string): boolean {
  const t = (text || "").trim()
  if (!t) return false
  if (CHALLENGE_HINT.test(t)) return true
  const lines = t
    .split(/\r?\n/)
    .map((ln) => ln.trim())
    .filter(Boolean)
  return lines.length >= MIN_LINES && t.length >= MIN_CHARS && CTF_PROSE.test(t)
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

/** Whether this prompt should trigger daemon load (path and/or challenge paste). */
export function shouldLoadChallengeFromPrompt(text: string): boolean {
  const raw = (text || "").trim()
  if (!raw) return false
  const paths = extractChallengePaths(raw)
  if (paths.length > 0) return true
  return looksLikeChallengePaste(raw)
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
