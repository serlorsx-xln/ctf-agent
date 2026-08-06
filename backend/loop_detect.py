"""Tool signature tracking for loop detection + resource-failure stuckness."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class LoopDetector:
    """Track recent tool call signatures to detect repetitive loops."""

    window: int = 12
    warn_threshold: int = 3
    break_threshold: int = 5
    fail_window: int = 10
    fail_break_threshold: int = 3
    _recent: deque[str] = field(init=False)
    _fail_classes: deque[str] = field(init=False)

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.window)
        self._fail_classes = deque(maxlen=self.fail_window)

    def check(self, tool_name: str, args: Mapping[str, object] | str | None = None) -> str | None:
        """Check if the agent is stuck in a loop.

        Returns:
            None: no loop
            "warn": approaching loop threshold
            "break": exceeded loop threshold, should force-break
        """
        if args:
            if isinstance(args, Mapping):
                raw = json.dumps(dict(args), sort_keys=True, default=str)
            else:
                raw = str(args)
            # Soften huge heredoc bodies so tiny edits still count as the same stuck cmd.
            raw = re.sub(r"<<['\"]?\w+['\"]?.*?^\w+$", "<<HEREDOC>>", raw, flags=re.S | re.M)
            sig = f"{tool_name}:{raw[:500]}"
        else:
            sig = tool_name
        self._recent.append(sig)

        count = sum(1 for s in self._recent if s == sig)
        if count >= self.break_threshold:
            return "break"
        if count >= self.warn_threshold:
            return "warn"
        return None

    def check_result(self, tool_name: str, result_text: str) -> str | None:
        """Track real memory deaths across different commands.

        Returns \"oom_break\" when the agent keeps OOMing. Network/tool
        timeouts (exit 124) are *not* treated as a resource loop — those are
        common on lab RPCs and must not push the agent off a valid path.
        """
        cls = classify_resource_failure(result_text)
        if cls != "oom":
            return None
        self._fail_classes.append(f"{tool_name}:{cls}")
        n = sum(1 for s in self._fail_classes if s.endswith(f":{cls}"))
        if n >= self.fail_break_threshold:
            return "oom_break"
        return None

    def reset(self) -> None:
        self._recent.clear()
        self._fail_classes.clear()


def classify_resource_failure(text: str) -> str | None:
    """Classify real resource deaths — not docs that merely mention OOM/137."""
    t = (text or "").lower()
    # Require concrete failure signals. Bare "oom"/"137" match /tools.txt docs
    # and falsely push agents off Sage onto pure-Python rewrites.
    if (
        "[exit 137]" in t
        or "out of memory" in t
        or "oom-kill" in t
        or "oom killed" in t
        or "killed (oom)" in t
    ):
        return "oom"
    # Timeouts are reported via RESOURCE_HINTS only — do not classify as a
    # stuckness class (lab network / RPC timeouts are not "resource loops").
    if "[exit 124]" in t or "command timed out" in t:
        return "timeout"
    return None


LOOP_WARNING_MESSAGE = (
    "⚠️ **You are stuck in a loop** — the exact same command has been repeated "
    "with identical results. Change arguments, targets, or tool flags before "
    "abandoning the technique. If grepping/searching, try a short Python script; "
    "if analyzing one aspect, switch to another surface."
)

OOM_STUCK_MESSAGE = (
    "⚠️ **Memory failure loop** — recent commands were OOM-killed (exit 137). "
    "Do NOT retry the same heavy approach. Shrink the work, stream/chunk results, "
    "or use a lighter installed tool from `/challenge/TOOLS.txt`."
)

RESOURCE_HINTS = {
    137: (
        "Hint: exit 137 is SIGKILL (OOM or hard-kill). "
        "If the command was hanging on the network, shrink timeouts / scan fewer ports; "
        "otherwise shrink memory use and check /challenge/TOOLS.txt."
    ),
    124: (
        "Hint: exit 124 is a timeout (often network/RPC, not a dead technique). "
        "Increase timeout, fix targets/flags (e.g. -target-ip / -dc-host), or narrow "
        "scope — do not abandon a working approach solely because of a timeout."
    ),
}
