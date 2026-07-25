/** Client-side credential sanity checks (mirrors server artemis-auth junk rules). */
const JUNK = new Set([
  "test",
  "xxx",
  "asdf",
  "foo",
  "bar",
  "password",
  "apikey",
  "api_key",
  "changeme",
  "placeholder",
  "your_key",
  "your-key",
  "sk-ant-...",
  "cursor_...",
  "crsr_...",
])

export function isJunkCredential(key: string | undefined, baseURL?: string): boolean {
  const k = (key || "").trim()
  if (!k || k.length < 8) return true
  if (JUNK.has(k.toLowerCase())) return true
  if (baseURL) {
    const u = baseURL.trim()
    if (!/^https?:\/\//i.test(u)) return true
    try {
      // eslint-disable-next-line no-new
      new URL(u)
    } catch {
      return true
    }
  }
  return false
}
