#!/usr/bin/env bash
# Split backup/cursor-backend-full into stacked themed branches from a1f3092.
set -euo pipefail
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
BASE=a1f3092
BACKUP=backup/cursor-backend-full

checkout_from_backup() {
  git checkout "$BACKUP" -- "$@"
}

commit_msg() {
  local msg="$1"
  shift
  git add "$@"
  git commit -m "$msg"
}

# --- 1. sandbox / setup ---
git checkout -B feat/sandbox-setup "$BASE"
SANDBOX_PATHS=(
  .dockerignore .env.example
  backend/launch_setup.py backend/sandbox/ backend/cli.py backend/tool_router.py
  sandbox/
  scripts/install.sh scripts/install.ps1 scripts/test-install.sh scripts/verify-install.sh
  scripts/pack-installer.sh scripts/pre_tui_setup.py scripts/build-tui.sh scripts/smoke_packs.py
  chassis/bin/artemis chassis/bin/artemis.cmd
  docs/ARCHITECTURE-SANDBOX.md
  tests/test_docker_hygiene.py tests/test_launch_setup.py tests/test_setup_ready.py
  tests/test_warm_runtime.py tests/test_l0_fast_bootstrap.py tests/test_setup_bake.py
  tests/test_pack_cache_lock.py tests/test_runtime_l0_image.py tests/test_container_paths.py
  tests/test_daemon_setup_gate.py
  chassis/packages/tui/src/component/dialog-setup-install.tsx
)
checkout_from_backup "${SANDBOX_PATHS[@]}"
commit_msg "feat(sandbox): setup gate, warm runtime, and Docker hygiene.

Add launch_setup, setup_ready, warm_runtime, docker_hygiene, install script
fixes, sandbox Dockerfiles, and in-TUI Install dialog with daemon tests." \
  "${SANDBOX_PATHS[@]}" scripts/split-branches.sh

# --- 2. operator / hold / qa ---
git checkout -B feat/operator-hold-qa
OPERATOR_PATHS=(
  backend/operator_inbox.py backend/agents/soft_steer.py
  backend/agents/swarm.py backend/agents/claude_solver.py backend/agents/codex_solver.py
  backend/agents/cursor_solver.py backend/agents/cursor_runtime.py backend/agents/gemini_solver.py
  backend/agents/live_log.py backend/continue_prompt.py backend/tools/core.py backend/flags.py
  backend/writeup.py backend/daemon/ backend/shell/bridge.py backend/deps.py backend/challenge_paste.py
  tests/test_operator_inbox.py tests/test_operator_queue_vs_steer.py tests/test_soft_steer.py
  tests/test_swarm_cancel_promotes_hold.py tests/test_swarm_flag_confirm_race.py tests/test_swarm_hold_qa.py
  tests/test_flags.py tests/test_continue_prompt.py tests/test_writeup.py tests/test_solve_attribution.py
  tests/test_gemini_solver.py tests/test_live_log.py tests/test_daemon_operator_message.py
  tests/test_e2e_product_contracts.py tests/test_adopt_pid_identity.py tests/test_wait_pid_gone.py
  tests/test_stdio_spawn_hygiene.py tests/test_vision_and_cursor_errors.py tests/fixtures/
)
checkout_from_backup "${OPERATOR_PATHS[@]}"
commit_msg "feat(swarm): operator inbox, Hold/Q&A, and flag-submit hardening.

Add soft steer, operator_inbox, Hold promote after CORRECT, targets_match,
ALREADY SOLVED guards, session_sync continue prompts, and swarm/daemon tests." \
  "${OPERATOR_PATHS[@]}"

# --- 3. TUI feed / session / client ---
git checkout -B feat/tui-feed-session
TUI_PATHS=(
  chassis/packages/tui/src/artemis/
  chassis/packages/tui/src/routes/session/index.tsx
  chassis/packages/tui/src/util/artemis-agent-key.ts
  chassis/packages/tui/src/util/artemis-challenge-paste.ts
  chassis/packages/tui/src/util/artemis-live-log.ts
  chassis/packages/tui/src/util/artemis-prompt-intent.ts
  chassis/packages/tui/src/util/artemis-solve-flow.ts
  chassis/packages/tui/src/util/artemis-solve-state.ts
  chassis/packages/tui/src/util/artemis-swarm-agents.ts
  chassis/packages/tui/src/util/artemis-tui-load.ts
  chassis/packages/tui/src/util/artemis-models.ts
  chassis/packages/tui/src/util/artemis-model-options.ts
  chassis/packages/tui/src/util/artemis-credentials.ts
  chassis/packages/tui/src/util/format.ts
  chassis/packages/tui/src/component/flag-confirm-bar.tsx
  chassis/packages/tui/src/component/swarm-agent-footer.tsx
  chassis/packages/tui/src/component/operator-queue-bar.tsx
  chassis/packages/tui/src/component/dialog-operator-delivery.tsx
  chassis/packages/tui/src/config/keybind.ts
  chassis/packages/tui/bunfig.toml
  chassis/packages/tui/test/util/artemis-agent-key.test.ts
  chassis/packages/tui/test/util/artemis-client-load.test.ts
  chassis/packages/tui/test/util/artemis-client-reconnect.test.ts
  chassis/packages/tui/test/util/artemis-prompt-intent.test.ts
  chassis/packages/tui/test/util/artemis-solve-flow.test.ts
  chassis/packages/tui/test/util/artemis-tui-load.test.ts
  chassis/packages/tui/test/util/artemis-live-log.test.ts
  chassis/packages/tui/test/util/artemis-solve-state.test.ts
  chassis/packages/tui/test/util/artemis-swarm-agents.test.ts
  tests/test_daemon_dialog_grace.py tests/test_daemon_multi_session.py
  tests/test_daemon_solve_flow.py tests/test_daemon_supervisor.py tests/conftest.py
)
checkout_from_backup "${TUI_PATHS[@]}"
commit_msg "feat(tui): session feed, client reconnect, and solve-flow UX.

Wire operator queue/Hold visibility, flag confirm decline on session switch,
failed-load session guard, reconnect elapsed, solve-flow gate tests, and
daemon dialog/session tests." \
  "${TUI_PATHS[@]}"

# --- 4. dead code / docs / qa scrub ---
git checkout -B chore/dead-code-cleanup
CHORE_PATHS=(
  README.md docs/TUI-PRODUCT-FLOW.md chassis/AGENTS.md scripts/qa.sh
  chassis/packages/tui/src/app.tsx chassis/packages/tui/src/context/sync.tsx
  chassis/packages/tui/src/feature-plugins/
  chassis/packages/opencode/bunfig.toml chassis/packages/opencode/specs/tui-plugins.md
  chassis/packages/opencode/src/plugin/tui/internal.ts chassis/packages/opencode/src/plugin/tui/runtime.ts
  chassis/packages/opencode/test/cli/tui/plugin-toggle.test.ts
  chassis/packages/opencode/test/config/tui.test.ts
  chassis/packages/script/src/index.ts
)
checkout_from_backup "${CHORE_PATHS[@]}"
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  git rm -f "$f" 2>/dev/null || rm -f "$f"
done < <(git diff --name-only --diff-filter=D "$BASE" "$BACKUP")
git add -u
commit_msg "chore(tui): remove unused OpenCode plugins and align docs.

Drop diff-viewer, which-key, session footer, and other dead Artemis paths;
dynamic-import theme picker; expand qa AppleDouble scrub; update README and
TUI-PRODUCT-FLOW." \
  "${CHORE_PATHS[@]}"

git branch -f cursor-backend HEAD
git checkout cursor-backend

echo "Done. Branches:"
git log --oneline --decorate a1f3092..HEAD
echo ""
echo "Diff vs backup (expect scripts/split-branches.sh only):"
git diff --stat backup/cursor-backend-full HEAD
