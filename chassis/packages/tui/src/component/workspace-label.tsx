import { useTheme } from "../context/theme"

export type WorkspaceStatus = "connected" | "connecting" | "disconnected" | "error"

export function WorkspaceLabel(props: { type: string; name: string; status?: WorkspaceStatus; icon?: boolean }) {
  const { theme } = useTheme()
  const color = () => {
    if (props.status === "connected") return theme.success
    if (props.status === "error") return theme.error
    return theme.textMuted
  }

  // No bare " " between spans — OpenTUI crashes on orphan text under <box>
  // (and this component is often nested inside <text>).
  return (
    <>
      {props.icon ? <span style={{ fg: color() }}>● </span> : undefined}
      <span style={{ fg: theme.text }}>{props.name}</span>
      <span style={{ fg: theme.textMuted }}> ({props.type})</span>
    </>
  )
}
