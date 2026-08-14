/** Parse swarm / solver stdout into agent-CLI style events for TUI rendering. */

export type ArtemisEvent =
  | { kind: "think"; agent: string; text: string }
  | { kind: "ai"; agent: string; text: string }
  | { kind: "bash"; agent: string; command: string }
  | { kind: "tool"; agent: string; tool: string; detail: string }
  | { kind: "result"; agent: string; text: string }
  | { kind: "flag_confirm"; id: string; flag: string }
  | { kind: "flags_ask"; id: string; default: number; challenge?: string }
  | { kind: "boot"; text: string; agent?: string }
  | { kind: "outcome"; level: "success" | "warn" | "error" | "info"; text: string; agent?: string }
  | { kind: "status"; text: string; agent?: string }
  | { kind: "summary"; text: string }
  | { kind: "turn"; text: string }

export function parseArtemisEvents(raw: string): ArtemisEvent[] {
  if (!raw) return []
  const out: ArtemisEvent[] = []
  for (const line of raw.split(/\r?\n/)) {
    const ev = parseLine(line)
    if (!ev) continue
    if (ev.kind === "summary") {
      for (const piece of expandSummaryLine(ev.text)) {
        out.push({ kind: "summary", text: piece })
      }
      continue
    }
    out.push(ev)
  }
  return dedupeEvents(coalesceEvents(out))
}

/** Split jammed numbered / section summary lines; strip markdown bold. */
export function expandSummaryLine(line: string): string[] {
  let text = (line || "").replace(/\*\*([^*]+)\*\*/g, "$1").trim()
  if (!text) return []
  const lead = (line || "").match(/^\s*/)?.[0] ?? ""
  // `…} ## Solution Summary 1. …` → plain section label on its own line.
  text = text
    .replace(
      /\s*#{1,3}\s*((?:Solution summary|Key insight|Challenge|How|Steps|What I tried|Why it worked|Dead ends)\b\s*:?)/gi,
      "\n$1\n",
    )
    .trim()
  const sectionRe =
    /(?=\b(?:Solution summary|Key insight|Challenge|What I tried|Why it worked|Dead ends)\b\s*:?|\b(?:How|Steps)\s*:)/i
  const sections = text
    .split(sectionRe)
    .map((s) => s.replace(/^#{1,3}\s+/, "").trim())
    .filter(Boolean)
  const chunks = sections.length ? sections : [text]
  const out: string[] = []
  // Only treat "N. " as a list marker at start/after whitespace — not in
  // CIPHER_PART_1/2. style tokens.
  const numberedRe = /(?:^|(?<=\s))(\d{1,2})\.\s+/g
  for (const section of chunks) {
    const matches = [...section.matchAll(numberedRe)]
    const pieces: string[] = []
    if (matches.length <= 1) {
      pieces.push(section)
    } else {
      const first = matches[0]!
      const head = section.slice(0, first.index).trim()
      if (head) pieces.push(head)
      for (let i = 0; i < matches.length; i++) {
        const start = matches[i]!.index!
        const end = i + 1 < matches.length ? matches[i + 1]!.index! : section.length
        const piece = section.slice(start, end).trim()
        if (piece) pieces.push(piece)
      }
    }
    for (const piece of pieces) {
      const parts = piece.split(/(?=\bDecryption\s*:)/i).map((s) => s.trim()).filter(Boolean)
      out.push(...parts)
    }
  }
  if (out.length === 1 && out[0] === text && lead) return [lead + text]
  return out
}

/** Model post-solve walls that belong in ``[artemis] summary``, not AI/think. */
export function looksLikeWriteupDump(text: string): boolean {
  const t = text || ""
  if (/(?:#{1,3}\s*)?Solution\s+Summary\b/i.test(t)) return true
  if (/\*\*FLAG\s*:/i.test(t) && /\d{1,2}\.\s/.test(t)) return true
  if (/FLAG\s*:\s*(?:flag|ctf|archa)\{/i.test(t) && /Solution\s+Summary/i.test(t)) return true
  if (/^The flag was accepted\b/i.test(t)) return true
  if (/\*\*XOR decryption\*\*/i.test(t)) return true
  if (/(?:^|\s)\d{1,2}\.\s+\*\*[^*]+\*\*\s*[—\-–]/i.test(t)) return true
  return false
}

function maybeExpandProse(text: string): string {
  const pieces = expandSummaryLine(text)
  return pieces.length > 1 ? pieces.join("\n") : text
}

/** Merge consecutive think/ai deltas from the same agent into one block. */
export function coalesceEvents(events: ArtemisEvent[]): ArtemisEvent[] {
  const out: ArtemisEvent[] = []
  for (const ev of events) {
    const last = out[out.length - 1]
    if (
      last &&
      (ev.kind === "think" || ev.kind === "ai") &&
      last.kind === ev.kind &&
      last.agent === ev.agent
    ) {
      last.text = joinThink(last.text, ev.text)
      continue
    }
    out.push(ev.kind === "think" || ev.kind === "ai" ? { ...ev } : ev)
  }
  return out
}

/**
 * Drop duplicate confirm / outcome lines (stdout+stderr merge, or
 * `[artemis] outcome` + submit_flag tool result carrying the same text).
 */
export function dedupeEvents(events: ArtemisEvent[]): ArtemisEvent[] {
  const seenOutcome = new Set<string>()
  const seenConfirm = new Set<string>()
  const out: ArtemisEvent[] = []
  const normOutcome = (text: string) => {
    let t = text
      .replace(/^\[artemis\]\s+outcome\s+/i, "")
      .replace(/^ERROR\s*[—\-–]\s*/i, "")
      .replace(/^>>>\s*/i, "")
      .trim()
    // One Confirmed line is enough — ignore via-suffix / punctuation drift.
    if (/^Confirmed\b/i.test(t)) {
      return "confirmed"
    }
    // Collapse quota lines that differ only by reset date / agent prefix.
    if (/usage limit|spend limit|quota exhausted|hit your usage/i.test(t)) {
      t = t
        .replace(/^[^\s]+\s+(?=Cursor\b)/i, "")
        .replace(/\s*\(resets?\s+[^)]+\)/gi, "")
        .replace(/\s*\(\d{1,2}\/\d{1,2}(?:\/\d{2,4})?\)/g, "")
        .replace(/\s*[—\-–]\s*/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .toLowerCase()
    }
    return t
  }
  for (const ev of events) {
    if (ev.kind === "outcome") {
      const key = `${ev.agent ?? ""}|${normOutcome(ev.text)}`
      if (seenOutcome.has(key)) continue
      // Drop FLAG FOUND once CORRECT is already on screen — same flag, louder.
      if (
        /^FLAG FOUND\b/i.test(ev.text) &&
        [...seenOutcome].some((k) => /\|CORRECT\b/i.test(k))
      ) {
        continue
      }
      seenOutcome.add(key)
    } else if (ev.kind === "flag_confirm") {
      if (seenConfirm.has(ev.id)) continue
      seenConfirm.add(ev.id)
    } else if (ev.kind === "status") {
      const key = normOutcome(ev.text)
      if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text) && seenOutcome.has(key))
        continue
      if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text)) seenOutcome.add(key)
    }
    out.push(ev)
  }
  return out
}

function joinThink(a: string, b: string): string {
  // Concatenate stream deltas as-is. Never insert spaces — SDK chunks already
  // include whitespace when needed. Inserting spaces between alnum pieces
  // breaks flags (`fl ag{…}`) and hex when flush boundaries split mid-token.
  if (!a) return b
  if (!b) return a
  return a + b
}

/** Strip logging prefixes like `19:52:47 INFO ` or `[19:52:47] INFO     `. */
export function stripLogPrefix(line: string): string {
  let s = line.trimEnd()
  s = s.replace(/^\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s+(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+/i, "")
  s = s.replace(/^\[\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\]\s+(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+/i, "")
  s = s.replace(/^(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+/i, "")
  // Rich markup leftovers from CLI console.print
  s = s.replace(/\x1b\[[0-9;]*m/g, "")
  return s.trimEnd()
}

export function parseLine(line: string): ArtemisEvent | null {
  const trimmed = stripLogPrefix(line)
  if (!trimmed) return null
  if (shouldDrop(trimmed)) return null

  if (/^\[artemis\]\s+usage\b/i.test(trimmed)) return null

  // [artemis] outcome CORRECT — …
  let m = trimmed.match(/^\[artemis\]\s+outcome\s+(.+)$/i)
  if (m) {
    const body = m[1]!.trim()
    return parseOutcome(body) || { kind: "outcome", level: "info", text: body }
  }

  // [artemis] boot …
  m = trimmed.match(/^\[artemis\]\s+boot\s+(.+)$/i)
  if (m) return { kind: "boot", text: m[1]!.trim() }

  // [artemis] summary … — end-of-run recap (who solved it and how).
  // Single literal space so the recap keeps its own indentation.
  m = trimmed.match(/^\[artemis\]\s+summary (.*)$/i)
  if (m) return m[1] ? { kind: "summary", text: m[1] } : null

  // [artemis] FLAGS_ASK id=abc default=1 challenge=name
  m = trimmed.match(
    /^\[artemis\]\s+FLAGS_ASK\s+id=(\S+)\s+default=(\d+)(?:\s+challenge=(.+))?$/i,
  )
  if (m) {
    return {
      kind: "flags_ask",
      id: m[1]!,
      default: Math.max(1, Math.min(64, Number(m[2]) || 1)),
      challenge: (m[3] || "").trim() || undefined,
    }
  }

  // [artemis] FLAG_CONFIRM id=abc flag=CTF{...}
  m = trimmed.match(/^\[artemis\]\s+FLAG_CONFIRM\s+id=(\S+)\s+flag=(.+)$/i)
  if (m) return { kind: "flag_confirm", id: m[1]!, flag: m[2]!.trim() }

  if (/FLAG CANDIDATE/i.test(trimmed) && /TUI dialog|confirm/i.test(trimmed)) {
    return { kind: "boot", text: "Waiting for flag confirmation…" }
  }

  // Outcome lines (flag progress) — before generic status.
  // Skip bracket-tagged agent lines so per-agent quota keeps ``agent``.
  if (!trimmed.startsWith("[")) {
    const outcome = parseOutcome(trimmed)
    if (outcome) return outcome
  }

  m = trimmed.match(/^\[([^\]]+?)\s+think\]\s*(.*)$/i)
  if (m) {
    const text = m[2]?.trim()
    if (!text) return null
    if (isGarbledModelText(text)) return null
    // Post-solve model dumps belong in ``[artemis] summary``, not think.
    if (looksLikeWriteupDump(text)) return null
    return { kind: "think", agent: shortAgent(m[1]!), text: maybeExpandProse(text) }
  }

  // Token-streamed writeup deltas — drop; the operator recap is ``[artemis] summary``.
  if (/^\[[^\]]+?\s+writeup\]/i.test(trimmed)) return null

  m = trimmed.match(/^\[([^\]]+?)\s+ai\]\s*(.*)$/i)
  if (m) {
    const text = m[2]?.trim()
    if (!text) return null
    if (isGarbledModelText(text)) return null
    // Jammed FLAG / Solution Summary walls — How: owns the recap.
    if (looksLikeWriteupDump(text)) return null
    return { kind: "ai", agent: shortAgent(m[1]!), text: maybeExpandProse(text) }
  }

  m = trimmed.match(/^\[([^\]]+?)\s+tool#\d+\s*(?:→|->)\s*(\w+)\]\s*(.*)$/i)
  if (m) {
    const tool = m[2]!
    const rest = m[3] || ""
    const cmd = extractCommand(tool, rest)
    if (cmd && tool === "bash") return { kind: "bash", agent: shortAgent(m[1]!), command: cmd }
    if (cmd) return { kind: "tool", agent: shortAgent(m[1]!), tool, detail: cmd }
    return { kind: "tool", agent: shortAgent(m[1]!), tool, detail: compactJson(rest) }
  }

  m = trimmed.match(/^\[([^\]]+?)\s+tool#\d+\s*(?:←|<-)\s*(\w+)\]\s*(.*)$/i)
  if (m) {
    const tool = m[2]!
    const body = (m[3] || "").trim()
    if (!body) return null
    const cleaned = cleanResult(body)
    const fromResult = parseOutcome(cleaned)
    if (fromResult) return fromResult
    if (tool === "submit_flag") {
      const flagOut = parseOutcome(cleaned) || parseSubmitDisplay(cleaned)
      if (flagOut) return flagOut
    }
    return { kind: "result", agent: shortAgent(m[1]!), text: truncate(cleaned, 4000) }
  }

  m = trimmed.match(/^\[([^\]]+?)\s+tool#\d+\s*(?:✗|x)\s*(\w+)\]\s*(.*)$/i)
  if (m) {
    const body = (m[3] || "").trim() || "failed"
    return { kind: "result", agent: shortAgent(m[1]!), text: truncate(`✗ ${m[2]} ${body}`, 4000) }
  }

  if (/providerIdentifier|custom-user-tools/i.test(trimmed)) return null
  if (/cursor-tool/i.test(trimmed)) return null

  m = trimmed.match(/^\[([^\]]+)\]\s*(──.*──)\s*$/)
  if (m) return { kind: "turn", text: m[2]! }

  if (/^\[(race|swarm) exit/i.test(trimmed)) {
    return { kind: "outcome", level: "info", text: trimmed.replace(/^\[race /i, "[swarm ") }
  }

  if (/^Starting (swarm|solvers)/i.test(trimmed)) return { kind: "boot", text: "Starting solvers…" }
  if (/Flags required set to/i.test(trimmed)) return { kind: "boot", text: trimmed }
  if (/TUI: flag candidates|auto-confirm/i.test(trimmed)) return { kind: "boot", text: trimmed }
  if (/already running|Stopped previous|stopping previous/i.test(trimmed)) {
    return { kind: "boot", text: trimmed }
  }
  if (/^>>>\s*(Confirmed|Rejected|Confirm)/i.test(trimmed)) {
    const t = trimmed.replace(/^>>>\s*/, "")
    if (/^Confirmed/i.test(t)) return { kind: "outcome", level: "success", text: t }
    if (/^Rejected/i.test(t)) return { kind: "outcome", level: "warn", text: t }
    return { kind: "status", text: t }
  }

  // Rich CLI banner lines → boot panel
  if (/^Artemis Swarm/i.test(trimmed)) return { kind: "boot", text: trimmed }
  if (/^(Models|Image|Challenge|Endpoint|Max challenges|Flag submit|Eval):/i.test(trimmed)) {
    return { kind: "boot", text: trimmed }
  }
  if (/\b(L0|prefetch|packs?=)/i.test(trimmed)) {
    const tagged = trimmed.match(/^\[([^\]]+)\]\s*(.+)$/)
    if (tagged && !isReservedAgentTag(tagged[1]!)) {
      const key = shortAgent(tagged[1]!)
      return { kind: "boot", text: truncate(`${key} ${tagged[2]!.trim()}`, 240), agent: key }
    }
    return { kind: "boot", text: truncate(trimmed, 200) }
  }

  m = trimmed.match(/^\[([^\]]+)\]\s*(.*)$/)
  if (m) {
    const body = (m[2] || "").trim()
    if (!body) return null
    if (shouldDrop(body)) return null
    if (isGarbledModelText(body)) return null
    if (/^(L0 |prefetch|Starting|Sandbox|distfiles|arch=)/i.test(body)) {
      const key = shortAgent(m[1]!)
      const reserved = isReservedAgentTag(m[1]!)
      return {
        kind: "boot",
        text: truncate(reserved ? body : `${key} ${body}`, 240),
        agent: reserved ? undefined : key,
      }
    }
    if (/usage limit|spend limit|quota exhausted|hit your usage/i.test(body)) {
      const reset = body.match(/reset[^\d]{0,40}?(\d{1,2}\/\d{1,2}(?:\/\d{2,4})?)/i)
      const short =
        "Cursor usage limit reached — switch model or wait for reset" +
        (reset ? ` (${reset[1]})` : "")
      // Reserved tags ([status], [INFO], …) are global — not a fake agent box.
      if (isReservedAgentTag(m[1]!)) {
        return { kind: "outcome", level: "error", text: short }
      }
      return { kind: "outcome", level: "error", text: short, agent: shortAgent(m[1]!) }
    }
    if (isReservedAgentTag(m[1]!)) {
      return { kind: "status", text: truncate(body, 240) }
    }
    return { kind: "status", text: truncate(`${shortAgent(m[1]!)} ${body}`, 240), agent: shortAgent(m[1]!) }
  }

  if (shouldDrop(trimmed)) return null
  if (isGarbledModelText(trimmed)) return null
  if (trimmed.length > 200 && !trimmed.startsWith("[")) return null

  return { kind: "status", text: truncate(trimmed, 200) }
}

function parseOutcome(text: string): Extract<ArtemisEvent, { kind: "outcome" }> | null {
  const t = text.trim()
  if (!t) return null
  // Shorten Cursor billing dumps that slipped through before humanize.
  if (/usage limit|spend limit|quota exhausted|hit your usage/i.test(t)) {
    const reset = t.match(/reset[^\d]{0,40}?(\d{1,2}\/\d{1,2}(?:\/\d{2,4})?)/i)
    const short =
      "Cursor usage limit reached — switch model or wait for reset" +
      (reset ? ` (${reset[1]})` : "")
    return { kind: "outcome", level: "error", text: short }
  }
  if (/^CORRECT\b/i.test(t) || /^FLAG FOUND\b/i.test(t)) {
    return { kind: "outcome", level: "success", text: t }
  }
  if (/^ACCEPTED\b/i.test(t) || /^Already accepted\b/i.test(t)) {
    return { kind: "outcome", level: "success", text: t }
  }
  if (/^REJECTED\b/i.test(t) || /^INCORRECT\b/i.test(t)) {
    return { kind: "outcome", level: "warn", text: t }
  }
  if (/^ERROR\b/i.test(t) || /^WARN\b/i.test(t)) {
    return { kind: "outcome", level: /^ERROR\b/i.test(t) ? "error" : "warn", text: t }
  }
  return null
}

function parseSubmitDisplay(text: string): Extract<ArtemisEvent, { kind: "outcome" }> | null {
  // submit_flag tool result often embeds the message
  if (/CORRECT|ACCEPTED|REJECTED|Already accepted/i.test(text)) {
    return parseOutcome(text) || { kind: "outcome", level: "info", text: truncate(text, 300) }
  }
  return null
}

function shouldDrop(s: string): boolean {
  const t = s.trim()
  if (!t) return true
  if (/^b['"]/.test(t)) return true
  if (/^\{['"]status['"]/.test(t)) return true
  if (/^\{"status"/.test(t)) return true
  if (/^={3,}/.test(t)) return true
  if (/^FLAG CANDIDATE — type y or n/i.test(t)) return true
  if (/providerIdentifier|custom-user-tools|cursor-tool/i.test(t)) return true
  if (/^[0-9a-f]{32,}$/i.test(t)) return true
  if (/^parts\s*=\s*\[/.test(t)) return true
  return false
}

/** Cursor quota / broken streams often emit mojibake (mixed CJK + Arabic, etc.). */
export function isGarbledModelText(text: string): boolean {
  const t = text.trim()
  if (t.length < 6) return false
  if (/\uFFFD/.test(t)) return true
  const cjk = (t.match(/[\u3400-\u9FFF]/g) || []).length
  const arabic = (t.match(/[\u0600-\u06FF]/g) || []).length
  const hangul = (t.match(/[\uAC00-\uD7AF]/g) || []).length
  const weird = cjk + arabic + hangul
  if (weird < 4) return false
  // Mixed scripts with little Latin → almost never real solver prose.
  const latin = (t.match(/[A-Za-z]/g) || []).length
  if (weird >= t.length * 0.4 && latin < 8) return true
  if (cjk > 0 && arabic > 0 && latin < 12) return true
  // Arabic-heavy bursts from blocked provider keys.
  if (arabic >= 6 && latin < 6 && arabic >= t.length * 0.35) return true
  return false
}

function cleanResult(body: string): string {
  let s = body.trim()
  try {
    const obj = JSON.parse(s) as Record<string, unknown>
    if (typeof obj.value === "string") return obj.value
    if (typeof obj.output === "string") return obj.output
    if (typeof obj.content === "string") return obj.content
  } catch {
    /* plain text */
  }
  const m = s.match(/['"]value['"]\s*:\s*['"](.+?)['"]\s*[,}]/)
  if (m?.[1]) return m[1]
  const first = s.split(/\r?\n/).map((l) => l.trim()).find(Boolean)
  return first || s
}

const AGENT_PROVIDERS = new Set([
  "cursor",
  "claude-sdk",
  "codex",
  "gemini-sdk",
  "anthropic",
  "openai",
  "google",
])
const AGENT_EFFORT = new Set(["low", "medium", "high", "xhigh", "max"])

function shortAgent(tag: string): string {
  let t = tag.trim().replace(/\s+(think|ai|tool)$/i, "")
  const parts = t.split("/").filter(Boolean)
  if (parts.length < 2) return parts[0] || tag
  // live() tags ``{challenge}/{model_id}``; logging tags ``{challenge}/{spec}``.
  let rest = parts.slice(1)
  if (rest.length >= 2 && AGENT_PROVIDERS.has(rest[0]!)) rest = rest.slice(1)
  if (rest.length && AGENT_EFFORT.has(rest[rest.length - 1]!)) rest = rest.slice(0, -1)
  return rest.join("/") || parts.at(-1) || tag
}

/** Bracket tags that are log/metadata, never swarm agent keys. */
function isReservedAgentTag(tag: string): boolean {
  const key = shortAgent(tag).toLowerCase()
  return (
    key === "status" ||
    key === "boot" ||
    key === "artemis" ||
    key === "swarm" ||
    key === "race" ||
    key === "info" ||
    key === "debug" ||
    key === "warning" ||
    key === "error" ||
    key === "critical"
  )
}

function extractCommand(tool: string, rest: string): string | undefined {
  const trimmed = rest.trim()
  if (!trimmed) return

  if (tool !== "bash") {
    try {
      const obj = JSON.parse(trimmed) as Record<string, unknown>
      if (typeof obj.path === "string") return String(obj.path)
      if (typeof obj.filename === "string") return String(obj.filename)
      if (typeof obj.flag === "string") return String(obj.flag)
    } catch {
      /* plain */
    }
    return truncate(trimmed, 4000)
  }

  try {
    const obj = JSON.parse(trimmed) as { command?: string; args?: { command?: string } }
    const cmd = obj.command || obj.args?.command
    if (typeof cmd === "string" && cmd.trim()) return truncate(cmd.trim(), 4000)
  } catch {
    const m = trimmed.match(/"command"\s*:\s*"((?:\\.|[^"\\])*)"/)
    if (m?.[1]) return truncate(m[1].replace(/\\n/g, " ").replace(/\\"/g, '"'), 800)
  }
  if (/<<\s*['"]?EOF/i.test(trimmed)) {
    const head = trimmed.split("\n")[0] || trimmed
    return truncate(head.replace(/\s+/g, " "), 300)
  }
  return truncate(trimmed.replace(/\s+/g, " "), 800)
}

function compactJson(rest: string): string {
  return truncate(rest.replace(/\s+/g, " "), 240)
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + "…"
}

/** True once the agent has started real work (think / tools) — leave boot panel. */
export function hasAgentActivity(events: ArtemisEvent[]): boolean {
  return events.some(
    (ev) =>
      ev.kind === "think" ||
      ev.kind === "ai" ||
      ev.kind === "bash" ||
      ev.kind === "tool" ||
      ev.kind === "result" ||
      ev.kind === "outcome" ||
      ev.kind === "flag_confirm",
  )
}

/** Think/tool stream only — excludes global outcomes (used for soft-race unlock). */
export function hasAgentWork(events: ArtemisEvent[]): boolean {
  return events.some(
    (ev) =>
      ev.kind === "think" ||
      ev.kind === "ai" ||
      ev.kind === "bash" ||
      ev.kind === "tool" ||
      ev.kind === "result",
  )
}

/** True when the event stream has a Cursor/account usage-limit error. */
export function hasQuotaOutcome(events: ArtemisEvent[]): boolean {
  return events.some((ev) => isQuotaText(ev.kind === "outcome" || ev.kind === "status" ? ev.text : ""))
}

function isQuotaText(text: string): boolean {
  return /usage limit|spend limit|quota exhausted|hit your usage/i.test(text)
}

/**
 * Account-level quota from ``[artemis] outcome ERROR — Cursor usage limit…``.
 * Per-agent quota outcomes carry ``agent`` and are NOT global (soft race may continue).
 */
export function isGlobalQuotaOutcome(ev: ArtemisEvent): boolean {
  if (ev.kind !== "outcome" || ev.level !== "error") return false
  if (!isQuotaText(ev.text)) return false
  if (ev.agent) return false
  return true
}

export function hasGlobalQuotaOutcome(events: ArtemisEvent[]): boolean {
  return events.some(isGlobalQuotaOutcome)
}

/** After CORRECT the prose recap owns the summary — hide late ai/think noise. */
export function dropPostSolveAgentChatter(events: ArtemisEvent[]): ArtemisEvent[] {
  let cut = -1
  for (let i = 0; i < events.length; i++) {
    const ev = events[i]!
    if (
      ev.kind === "outcome" &&
      ev.level === "success" &&
      /CORRECT|Challenge complete/i.test(ev.text)
    ) {
      cut = i
      break
    }
  }
  if (cut < 0) return events
  return events.filter((ev, i) => i <= cut || (ev.kind !== "ai" && ev.kind !== "think"))
}

/** Terminal solve outcome — spinner must stop even if swarm_exit is delayed.

 * Soft race: a **per-agent** mid-run quota/error is NOT global — siblings on other
 * providers may still run.
 *
 * Account-level ``[artemis] outcome`` usage-limit is ALWAYS terminal (Cursor billing
 * is shared across Cursor models; waiting on siblings only spins Solving forever).
 */
export function isTerminalSolveOutcome(ev: ArtemisEvent, multiAgent = false): boolean {
  if (ev.kind === "outcome") {
    if (ev.level === "success") {
      return /CORRECT|Challenge complete/i.test(ev.text) || /^FLAG FOUND\b/i.test(ev.text)
    }
    if (ev.level === "error") {
      if (isQuotaText(ev.text)) {
        if (isGlobalQuotaOutcome(ev)) return true
        return !multiAgent
      }
      // Non-quota errors mid soft-race stay local unless clearly a global stop.
      if (multiAgent) return false
      return true
    }
    return /swarm exit|No flag found|No flag accepted|Stopped:/i.test(ev.text)
  }
  if (ev.kind === "status") {
    return /swarm exit|No flag found|No flag accepted|Stopped:/i.test(ev.text)
  }
  return false
}

export function hasTerminalSolveOutcome(events: ArtemisEvent[], multiAgent = false): boolean {
  if (events.some((ev) => isTerminalSolveOutcome(ev, multiAgent))) return true
  // Soft race never started: any quota with zero agent think/tool/ai lines.
  if (multiAgent && hasQuotaOutcome(events) && !hasAgentWork(events)) return true
  return false
}
