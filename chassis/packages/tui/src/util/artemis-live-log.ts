import { isReservedAgentTag, shortAgent, targetsMatch } from "./artemis-agent-key"

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
  | { kind: "status"; text: string; agent?: string; delivery?: "steer" | "queue"; target?: string }
  | { kind: "summary"; text: string }
  | { kind: "turn"; text: string }
  /** Operator force-send into the live solve (visible in the solve feed). */
  | { kind: "operator"; text: string; delivery: "steer" | "queue"; target?: string }
  /** Invisible turn break between Hold Q&A replies (from qa-wait). */
  | { kind: "qa_sep" }

export function parseArtemisEvents(raw: string): ArtemisEvent[] {
  if (!raw) return []
  const out: ArtemisEvent[] = []
  for (const line of raw.split(/\r?\n/)) {
    out.push(...eventsFromLogLine(line))
  }
  return dedupeEvents(coalesceEvents(out))
}

/** One stdout line → zero or more events (qa_sep, expanded summary, …).

 * Shared by batch ``parseArtemisEvents`` and live ``appendSwarmLog`` so Hold
 * turn breaks are not test-only.
 */
export function eventsFromLogLine(line: string): ArtemisEvent[] {
  const ev = parseLine(line)
  if (!ev) return []
  const out: ArtemisEvent[] = []
  if (ev.kind === "status" && /^Answering/i.test(ev.text)) {
    out.push({ kind: "qa_sep" })
  }
  // End-of-answer marker — clears Hold answering spinner without a new qa-wait.
  if (ev.kind === "status" && /^Q&A done\b/i.test(ev.text)) {
    out.push({ kind: "qa_sep" })
    return out
  }
  if (ev.kind === "summary") {
    for (const piece of expandSummaryLine(ev.text)) {
      out.push({ kind: "summary", text: piece })
    }
    return out
  }
  out.push(ev)
  return out
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
function looksLikeWriteupDump(text: string): boolean {
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
 *
 * ``prior`` seeds the seen-sets without being re-emitted — used when
 * incremental merge only re-dedupes a short tail so older CORRECT still
 * suppresses later FLAG FOUND / Confirmed dupes.
 */
export function dedupeEvents(
  events: ArtemisEvent[],
  prior: ArtemisEvent[] = [],
): ArtemisEvent[] {
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
  const seed = (ev: ArtemisEvent) => {
    if (ev.kind === "outcome") {
      const bare = normOutcome(ev.text)
      seenOutcome.add(`${ev.agent ?? ""}|${bare}`)
      seenOutcome.add(bare)
    } else if (ev.kind === "flag_confirm") {
      seenConfirm.add(ev.id)
    } else if (ev.kind === "status") {
      if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text)) {
        const bare = normOutcome(ev.text)
        seenOutcome.add(bare)
        seenOutcome.add(`|${bare}`)
      }
    }
  }
  for (const ev of prior) seed(ev)
  for (const ev of events) {
    if (ev.kind === "outcome") {
      const key = `${ev.agent ?? ""}|${normOutcome(ev.text)}`
      const bare = normOutcome(ev.text)
      if (seenOutcome.has(key) || seenOutcome.has(bare)) continue
      // Drop FLAG FOUND once CORRECT is already on screen — same flag, louder.
      if (
        /^FLAG FOUND\b/i.test(ev.text) &&
        [...seenOutcome].some((k) => /(?:^|\|)CORRECT\b/i.test(k))
      ) {
        continue
      }
      seenOutcome.add(key)
      seenOutcome.add(bare)
    } else if (ev.kind === "flag_confirm") {
      if (seenConfirm.has(ev.id)) continue
      seenConfirm.add(ev.id)
    } else if (ev.kind === "status") {
      const key = normOutcome(ev.text)
      if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text) && seenOutcome.has(key))
        continue
      if (/Confirmed|CORRECT|FLAG FOUND|Challenge complete|usage limit/i.test(ev.text)) {
        seenOutcome.add(key)
        // Also seed agent| form so outcome/status cross-kind dedupe works.
        seenOutcome.add(`|${key}`)
      }
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

function parseLine(line: string): ArtemisEvent | null {
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

  // [artemis] hold — post-CORRECT Q&A with winning solver
  m = trimmed.match(/^\[artemis\]\s+hold\b(.*)$/i)
  if (m) {
    const rest = (m[1] || "").trim()
    if (/released/i.test(rest)) {
      return { kind: "status", text: "Hold released" }
    }
    return {
      kind: "status",
      text: rest
        ? `Hold${rest.startsWith("—") || rest.startsWith("-") ? " " + rest : ": " + rest}`
        : "Hold — ask follow-ups",
    }
  }

  // [artemis] qa-wait … — Hold Q&A model is thinking
  m = trimmed.match(/^\[artemis\]\s+qa-wait\b(.*)$/i)
  if (m) {
    const rest = (m[1] || "").trim().replace(/^[·\-–:]\s*/, "")
    return {
      kind: "status",
      text: rest ? `Answering… · ${rest}` : "Answering…",
    }
  }

  // [artemis] qa-done — end of one Hold answer (footer spinner off)
  if (/^\[artemis\]\s+qa-done\b/i.test(trimmed)) {
    return { kind: "status", text: "Q&A done" }
  }

  // [artemis] qa … — winning solver follow-up reply (main-feed status; not agent-scoped ai)
  // Optional ``→agent:`` tag for assistant-style footer (roster display key).
  // Keep body whitespace (do not .trim()) so markdown paragraphs / fences survive.
  // Only one optional space after ``:`` is the emit separator — further spaces are content.
  m = trimmed.match(/^\[artemis\]\s+qa\s+(?:→([^\s:]+)\s*: ?)?(.*)$/i)
  if (m) {
    const agent = (m[1] || "").trim() || undefined
    const body = m[2] ?? ""
    if (!body.trim()) {
      return {
        kind: "status",
        text: "Q&A · ",
        ...(agent ? { agent } : {}),
      }
    }
    if (body.trim() === "(no reply)") {
      return {
        kind: "status",
        text: "Q&A · (no reply)",
        ...(agent ? { agent } : {}),
      }
    }
    return { kind: "status", text: `Q&A · ${body}`, ...(agent ? { agent } : {}) }
  }

  // [artemis] you (steer|queue)[→target]: … — operator force-send
  m = trimmed.match(/^\[artemis\]\s+you\s*\((steer|queue)(?:→([^\)]+))?\)\s*:\s*(.*)$/i)
  if (m) {
    const delivery = m[1]!.toLowerCase() === "queue" ? "queue" : "steer"
    const targetRaw = (m[2] || "").trim()
    const target =
      !targetRaw || targetRaw === "all" ? undefined : targetRaw
    const body = (m[3] || "").trim()
    if (body) return { kind: "operator", text: body, delivery, ...(target ? { target } : {}) }
  }

  // [artemis] solver ← operator (steer|queue)[→target]: …
  m = trimmed.match(
    /^\[artemis\]\s+solver\s*←\s*operator\s*\((steer|queue)(?:→([^\)]+))?\)\s*:\s*(.*)$/i,
  )
  if (m) {
    const delivery = m[1]!.toLowerCase() === "queue" ? "queue" : "steer"
    const targetRaw = (m[2] || "").trim()
    const target =
      !targetRaw || targetRaw === "all" ? undefined : targetRaw
    const body = (m[3] || "").trim()
    if (body) {
      return {
        kind: "status",
        text: `Solver read · ${body}`,
        delivery,
        ...(target ? { target } : {}),
      }
    }
  }

  // [artemis] followup — … — force-followup progress (not a hang)
  m = trimmed.match(/^\[artemis\]\s+followup\s*[—\-–:]?\s*(.*)$/i)
  if (m) {
    const rest = (m[1] || "").trim()
    return { kind: "status", text: rest ? `Follow-up · ${rest}` : "Follow-up" }
  }

  // Legacy: [artemis] operator → swarm …
  m = trimmed.match(/^\[artemis\]\s+operator\s+→\s+swarm.*?:\s*(.*)$/i)
  if (m) {
    const body = (m[1] || "").trim()
    if (body) return { kind: "operator", text: body, delivery: "steer" }
  }

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

  // Custom tools use tool#N; Cursor SDK host Shell/Read may omit the step.
  m = trimmed.match(/^\[([^\]]+?)\s+tool(?:#\d+)?\s*(?:→|->)\s*([^\s\]]+)\]\s*(.*)$/i)
  if (m) {
    const tool = normalizeToolName(m[2]!)
    const rest = m[3] || ""
    const cmd = extractCommand(tool, rest)
    if (cmd && tool === "bash") return { kind: "bash", agent: shortAgent(m[1]!), command: cmd }
    if (cmd) return { kind: "tool", agent: shortAgent(m[1]!), tool, detail: cmd }
    return { kind: "tool", agent: shortAgent(m[1]!), tool, detail: compactJson(rest) }
  }

  m = trimmed.match(
    /^\[([^\]]+?)\s+tool(?:#\d+)?\s*(?:←|<-)\s*([^\s\]]+)(?:\s*\([^)]*\))?\]\s*(.*)$/i,
  )
  if (m) {
    const tool = normalizeToolName(m[2]!)
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

  m = trimmed.match(/^\[([^\]]+?)\s+tool(?:#\d+)?\s*(?:✗|x)\s*([^\s\]]+)\]\s*(.*)$/i)
  if (m) {
    const tool = normalizeToolName(m[2]!)
    const body = (m[3] || "").trim() || "failed"
    return { kind: "result", agent: shortAgent(m[1]!), text: truncate(`✗ ${tool} ${body}`, 4000) }
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

function normalizeToolName(name: string): string {
  const t = (name || "").trim()
  if (/^(bash|shell)$/i.test(t)) return "bash"
  return t
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
      if (typeof obj.command === "string") return String(obj.command)
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

/** True once the agent has started real work — leave boot panel.

 * Includes outcomes / flag confirm / operator notes so mid-solve chat and
 * CORRECT unlock the feed even without think/tool lines.
 */
export function hasAgentActivity(events: ArtemisEvent[]): boolean {
  return events.some(
    (ev) =>
      ev.kind === "think" ||
      ev.kind === "ai" ||
      ev.kind === "bash" ||
      ev.kind === "tool" ||
      ev.kind === "result" ||
      ev.kind === "outcome" ||
      ev.kind === "flag_confirm" ||
      ev.kind === "operator",
  )
}

/**
 * Status lines that belong on the solve feed (main or agent page).
 * Steer plumbing (Solver read / Follow-up / Answering) stays out — footer spins.
 */
export function isFeedVisibleStatus(text: string): boolean {
  const t = String(text || "")
  if (/^(Solver read|Follow-up|Answering|Q&A done)/i.test(t)) return false
  return /Waiting for flag|Confirmed|Rejected|swarm exit|Stopped:|No flag|Possible flag|submit_flag|Q&A|Hold\b/i.test(
    t,
  )
}

/**
 * Queue notes still waiting for an idle turn.
 * Cleared only by a matching ``Solver read`` with ``delivery: "queue"``.
 * Optional ``focus`` scopes to one agent page (broadcast notes always included).
 */
export function pendingOperatorQueue(
  events: ArtemisEvent[],
  focus?: string | null,
): string[] {
  const pending: { text: string; target?: string }[] = []
  const dequeue = (body: string, target?: string) => {
    if (!body) return
    const matches = (queued: string, read: string) => {
      if (queued === read) return true
      // Inbox emit truncates reads — only a proper prefix of the queued note
      // clears it. Never let a short read clear a longer distinct note.
      return read.length >= 32 && queued.startsWith(read)
    }
    const idx = pending.findIndex((p) => {
      if (target) {
        // Targeted Solver read may clear same-target (base↔#N) or legacy unscoped.
        if (p.target && !targetsMatch(p.target, target)) return false
      } else if (p.target) {
        // Unscoped read must not steal another agent's sticky queue.
        return false
      }
      return matches(p.text, body)
    })
    if (idx >= 0) pending.splice(idx, 1)
  }
  for (const ev of events) {
    if (ev.kind === "operator" && ev.delivery === "queue") {
      const t = String(ev.text || "").trim()
      if (!t) continue
      pending.push({ text: t, target: ev.target })
      continue
    }
    if (ev.kind !== "status" || ev.delivery !== "queue") continue
    const m = String(ev.text || "").match(/^Solver read ·\s*(.*)$/i)
    if (!m) continue
    dequeue((m[1] || "").trim(), ev.target)
  }
  const focusKey = (focus || "").trim() || null
  return pending
    .filter((p) => {
      if (!focusKey) return true
      // Agent page: show notes for this agent (and legacy unscoped).
      return !p.target || targetsMatch(p.target, focusKey)
    })
    .map((p) => p.text)
}

/**
 * Post-CORRECT winner Q&A hold is active (process intentionally alive).
 * Walks the stream so a later "Hold released" clears an earlier Hold banner.
 */
export function isSolverHoldActive(events: ArtemisEvent[]): boolean {
  let hold = false
  for (const ev of events) {
    if (ev.kind !== "status") continue
    const t = String(ev.text || "")
    if (/^Hold released\b/i.test(t)) {
      hold = false
      continue
    }
    if (/^Hold\b/i.test(t)) hold = true
  }
  return hold
}

/** Success terminal outcome (CORRECT / FLAG FOUND) — writeup + hold may still run. */
export function hasCorrectSolveOutcome(events: ArtemisEvent[]): boolean {
  return events.some(
    (ev) =>
      ev.kind === "outcome" &&
      ev.level === "success" &&
      (/CORRECT|Challenge complete/i.test(ev.text) || /^FLAG FOUND\b/i.test(ev.text)),
  )
}

/** Think/tool stream only — excludes global outcomes (used for soft-race unlock). */
function hasAgentWork(events: ArtemisEvent[]): boolean {
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
function hasQuotaOutcome(events: ArtemisEvent[]): boolean {
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

/** After CORRECT the prose recap owns the summary — hide late ai/think noise.

 * Hold Q&A replies are ``[artemis] qa …`` → ``kind: "status"`` (``Q&A · …``) so they
 * stay on the main feed. ``Answering…`` is footer-only (filtered from the feed).
 * Once Hold / a post-CORRECT operator note begins, also keep any late ai/think
 * from the winner session.
 *
 * Bash/tool/result after CORRECT are always dropped on the main feed — Hold Q&A
 * is conversational; dumping the last ``default`` tool card mid-answer is noise.
 */
export function dropPostSolveAgentChatter(events: ArtemisEvent[]): ArtemisEvent[] {
  let cut = -1
  for (let i = 0; i < events.length; i++) {
    const ev = events[i]!
    if (
      ev.kind === "outcome" &&
      ev.level === "success" &&
      (/CORRECT|Challenge complete/i.test(ev.text) || /^FLAG FOUND\b/i.test(ev.text))
    ) {
      cut = i
      break
    }
  }
  if (cut < 0) return events
  let keepQa = false
  return events.filter((ev, i) => {
    if (i <= cut) return true
    if (ev.kind === "bash" || ev.kind === "tool" || ev.kind === "result") return false
    if (ev.kind === "qa_sep") {
      keepQa = true
      return true
    }
    if (ev.kind === "operator") {
      keepQa = true
      return true
    }
    if (ev.kind === "status") {
      const t = String(ev.text || "")
      if (/^Hold\b/i.test(t) || /^Answering/i.test(t) || /^Q&A\b/i.test(t)) {
        keepQa = true
      }
      return true
    }
    if (ev.kind === "ai" || ev.kind === "think") return keepQa
    return true
  })
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
