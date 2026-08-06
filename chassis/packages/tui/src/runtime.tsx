import path from "path"
import { posix } from "path"

function pathFor(input: string, home: string) {
  // Unix-style paths on Windows (tests, WSL, SSH sessions) — use posix rules.
  if (process.platform === "win32" && !/^[A-Za-z]:/.test(input) && !/^[A-Za-z]:/.test(home)) {
    return posix
  }
  return path
}

export function abbreviateHome(input: string, home: string) {
  if (!home) return input
  const p = pathFor(input, home)
  const relative = p.relative(home, input)
  if (relative === "") return "~"
  if (relative === ".." || relative.startsWith(".." + p.sep) || p.isAbsolute(relative)) return input
  return "~" + p.sep + relative
}
