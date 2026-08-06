import { For } from "solid-js"
import { useTheme } from "../context/theme"
import { logo } from "../logo"

export function Logo() {
  const { theme } = useTheme()

  // Plain glyphs — flatten cells so OpenTUI never sees orphan whitespace under <box>.
  return (
    <box>
      <For each={logo.lines}>
        {(line) => {
          const cells = Array.from(line).map((char) => (
            <text fg={theme.text} selectable={false}>
              {char}
            </text>
          ))
          return <box flexDirection="row">{cells}</box>
        }}
      </For>
    </box>
  )
}
