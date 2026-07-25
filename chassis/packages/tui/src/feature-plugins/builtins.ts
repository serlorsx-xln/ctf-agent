import type { TuiPlugin, TuiPluginModule } from "@opencode-ai/plugin/tui"
import HomeFooter from "./home/footer"
import HomeTips from "./home/tips"
import SidebarChallenge from "./sidebar/challenge"
import SidebarContext from "./sidebar/context"
import SidebarFooter from "./sidebar/footer"
import SidebarMcp from "./sidebar/mcp"
import SidebarTodo from "./sidebar/todo"
import Notifications from "./system/notifications"
import PluginManager from "./system/plugins"

export type BuiltinTuiPlugin = Omit<TuiPluginModule, "id"> & {
  id: string
  tui: TuiPlugin
  enabled?: boolean
}

/** Artemis builtins: Challenge + Context + MCP + Todo + Footer (no LSP / Diff / WhichKey / Files). */
export function createBuiltinPlugins(_options: { experimentalEventSystem: boolean }): BuiltinTuiPlugin[] {
  return [
    HomeFooter,
    HomeTips,
    SidebarChallenge,
    SidebarContext,
    SidebarMcp,
    SidebarTodo,
    SidebarFooter,
    Notifications,
    PluginManager,
  ]
}
