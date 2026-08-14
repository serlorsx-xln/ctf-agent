import {
  hasGlobalQuotaOutcome,
  type ArtemisEvent,
} from "./artemis-live-log"

const EFFORT_SUFFIXES = new Set(["low", "medium", "high", "xhigh", "max"])

/** Display key aligned with backend ``model_id_from_spec`` / live-log ``shortAgent``.

 * Everything after the provider, minus effort. Slashy Claude ids stay intact
 * (``claude-sdk/aliyuncs/glm-5.2`` → ``aliyuncs/glm-5.2``). Taking only the
 * first segment left boxes named ``aliyuncs`` while logs tagged ``glm-5.2``.
 * Duplicate runners keep ``#N`` from ``assign_runner_ids``.
 */
export function agentKeyFromSpec(spec: string): string {
  const s = spec.trim()
  if (!s) return "agent"
  const hash = s.match(/#(\d+)$/)
  const base = hash ? s.slice(0, s.lastIndexOf("#")) : s
  const parts = base.split("/").filter(Boolean)
  if (parts.length < 2) return hash ? `${base}#${hash[1]}` : s
  let rest = parts.slice(1)
  if (rest.length && EFFORT_SUFFIXES.has(rest[rest.length - 1]!)) {
    rest = rest.slice(0, -1)
  }
  const id = rest.join("/") || parts[1]!
  return hash ? `${id}#${hash[1]}` : id
}

/**
 * Match backend ``assign_runner_ids`` then the live-log agent label.
 */
export function rosterFromSpecs(specs: string[]): string[] {
  const totals = new Map<string, number>()
  for (const s of specs) {
    const t = s.trim()
    if (!t) continue
    totals.set(t, (totals.get(t) ?? 0) + 1)
  }
  const seen = new Map<string, number>()
  const out: string[] = []
  for (const raw of specs) {
    const spec = raw.trim()
    if (!spec) continue
    const n = (seen.get(spec) ?? 0) + 1
    seen.set(spec, n)
    const rid = (totals.get(spec) ?? 1) === 1 ? spec : `${spec}#${n}`
    out.push(agentKeyFromSpec(rid))
  }
  return out
}

/** Box labels for a run: model specs win over a collapsed daemon agent list.

 * Old daemons labeled every ``claude-sdk/aliyuncs/…`` box ``aliyuncs``, so four
 * picks became two. Rebuild from the specs the operator actually confirmed.
 */
export function rosterFromDaemonPush(agents: string[], models: string[]): string[] {
  const fromModels = rosterFromSpecs(models)
  if (fromModels.length > 0) return fromModels
  return agents.map((a) => a.trim()).filter(Boolean)
}

function eventAgent(ev: ArtemisEvent): string | null {
  // Boot lines name their container's owner but are not that agent's work:
  // counting them would flip a still-booting box to active with no output.
  if (ev.kind === "boot") return null
  if ("agent" in ev && typeof ev.agent === "string" && ev.agent) return ev.agent
  return null
}

const RESERVED_AGENT_KEYS = new Set([
  "status",
  "boot",
  "artemis",
  "swarm",
  "race",
  "info",
  "debug",
  "warning",
  "error",
  "critical",
])

function isWorkEvent(ev: ArtemisEvent): boolean {
  return ev.kind === "think" || ev.kind === "ai" || ev.kind === "bash" || ev.kind === "tool" || ev.kind === "result"
}

/** Ordered agent keys: roster wins; else model specs; else work events in the log.

 * When the daemon/TUI has an explicit roster (current run), NEVER invent extra
 * boxes from leftover think/tool lines of a previous run (spawn 3 → flash 5).
 */
export function listSwarmAgents(
  events: ArtemisEvent[],
  modelSpecs: string[],
  roster: string[] = [],
): string[] {
  const ordered: string[] = []
  const seen = new Set<string>()
  const push = (key: string) => {
    if (!key || seen.has(key) || RESERVED_AGENT_KEYS.has(key.toLowerCase())) return
    seen.add(key)
    ordered.push(key)
  }
  const fromSpecs = rosterFromSpecs(modelSpecs)
  for (const key of roster) push(key)
  // Explicit roster is the current run — unless it is shorter than the pick
  // (old daemon collapsed ``aliyuncs/MiniMax-M2.1`` + ``aliyuncs/glm-4.7``).
  if (roster.length > 0 && ordered.length >= fromSpecs.length) return ordered
  if (fromSpecs.length > ordered.length) return fromSpecs
  if (roster.length > 0 || modelSpecs.length > 0) return ordered
  for (const ev of events) {
    if (!isWorkEvent(ev)) continue
    const a = eventAgent(ev)
    if (a) push(a)
  }
  return ordered
}

export function partitionEventsByAgent(events: ArtemisEvent[], agent: string): ArtemisEvent[] {
  return events.filter((ev) => eventAgent(ev) === agent)
}

/** Shared / global lines shown on the main swarm page (not owned by one agent). */
export function globalSwarmEvents(events: ArtemisEvent[]): ArtemisEvent[] {
  return events.filter((ev) => {
    // Interactive FlagConfirmBar owns y/n — skip passive log duplicate.
    if (ev.kind === "flag_confirm") return false
    // Per-agent outcomes belong on that agent's page.
    if (ev.kind === "outcome") return !ev.agent
    if (ev.kind === "flags_ask") return true
    if (ev.kind === "boot") return true
    // End-of-run recap belongs to the run, not to one agent's page.
    if (ev.kind === "summary") return true
    if (ev.kind === "status") {
      return /Waiting for flag|Confirmed|Rejected|swarm exit|Stopped:|No flag/i.test(ev.text)
    }
    return false
  })
}

/**
 * Agents credited with an accepted flag, read off the end-of-run recap.
 *
 * The backend emits box labels here, so these keys line up with the grid.
 */
export function winnerAgents(events: ArtemisEvent[]): string[] {
  for (const ev of events) {
    if (ev.kind !== "summary") continue
    const m = ev.text.match(/^Solved by (.+)$/i)
    if (!m) continue
    return m[1]!
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s && s !== "the swarm")
  }
  return []
}

export type AgentPreview = {
  agent: string
  lastLine: string
  active: boolean
  eventCount: number
  /** Agent hit an error / usage limit (or never started under account quota). */
  failed: boolean
  /** This agent submitted an accepted flag. */
  won: boolean
}

export function agentPreviews(
  events: ArtemisEvent[],
  agents: string[],
  swarmRunning: boolean,
  lineCounts: Record<string, number> = {},
): AgentPreview[] {
  const accountQuota = hasGlobalQuotaOutcome(events)
  const winners = new Set(winnerAgents(events))
  return agents.map((agent) => {
    const mine = partitionEventsByAgent(events, agent)
    const last = mine[mine.length - 1]
    let lastLine = swarmRunning && mine.length === 0 ? "starting…" : "waiting…"
    if (last) {
      if (last.kind === "think") lastLine = last.text
      else if (last.kind === "ai") lastLine = last.text
      else if (last.kind === "bash") lastLine = `$ ${last.command}`
      else if (last.kind === "tool") lastLine = `${last.tool} ${last.detail}`
      else if (last.kind === "result") lastLine = last.text
      else if (last.kind === "status") lastLine = last.text
      else if (last.kind === "outcome") lastLine = last.text
    }
    const agentFailed = mine.some(
      (ev) =>
        (ev.kind === "outcome" && ev.level === "error") ||
        (ev.kind === "status" && /usage limit|spend limit|quota exhausted|hit your usage|gave up/i.test(ev.text)),
    )
    // While the swarm is still running, only mark agents that themselves failed.
    // Painting every zero-event sibling as "usage limit · failed" makes a not-yet-
    // started roster look like a total quota death (operator: not started ≠ no quota).
    // After swarm_exit, account-level Cursor quota may mark the whole grid failed.
    const won = winners.has(agent)
    const failed = !won && (agentFailed || (accountQuota && !swarmRunning))
    if (won) {
      // Nothing else on the box says who actually earned the flag.
      lastLine = "solved · flag accepted"
    } else if (agentFailed) {
      // Prefer the concrete outcome text when we have one; otherwise a short
      // failure label. Never keep the last bash/think line — that is what made
      // a quota death look like the agent was still working.
      const failLine =
        last?.kind === "outcome" && last.level === "error"
          ? last.text
          : last?.kind === "status" && /usage limit|spend limit|quota|gave up/i.test(last.text)
            ? last.text
            : "usage limit · failed"
      lastLine = failLine
    } else if (accountQuota && !swarmRunning) {
      lastLine = "usage limit · failed"
    }
    if (lastLine.length > 120) lastLine = lastLine.slice(0, 119) + "…"
    // Prefer monotonic line counts — coalesced think/ai must not shrink the badge.
    const eventCount = Math.max(mine.length, lineCounts[agent] ?? 0)
    return {
      agent,
      lastLine,
      active: swarmRunning && mine.length > 0 && !failed,
      eventCount,
      failed,
      won,
    }
  })
}
