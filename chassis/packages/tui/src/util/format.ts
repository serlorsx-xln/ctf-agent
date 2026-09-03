export function formatDuration(secs: number) {
  if (secs <= 0) return ""
  if (secs < 60) return `${secs}s`
  if (secs < 3600) {
    const mins = Math.floor(secs / 60)
    const remaining = secs % 60
    return remaining > 0 ? `${mins}m ${remaining}s` : `${mins}m`
  }
  if (secs < 86400) {
    const hours = Math.floor(secs / 3600)
    const remaining = Math.floor((secs % 3600) / 60)
    return remaining > 0 ? `${hours}h ${remaining}m` : `${hours}h`
  }
  if (secs < 604800) {
    const days = Math.floor(secs / 86400)
    return days === 1 ? "~1 day" : `~${days} days`
  }
  const weeks = Math.floor(secs / 604800)
  return weeks === 1 ? "~1 week" : `~${weeks} weeks`
}

/** Swarm footer / Solve card elapsed — freezes when ``running`` is false. */
export function formatSwarmElapsed(
  startedAt: number | null | undefined,
  endedAt: number | null | undefined,
  now: number,
  running: boolean,
): string | null {
  if (startedAt == null) return null
  const end = running ? now : (endedAt ?? now)
  const secs = Math.max(0, Math.floor((end - startedAt) / 1000))
  if (secs < 60) return `${secs}s`
  return formatDuration(secs) || `${secs}s`
}
