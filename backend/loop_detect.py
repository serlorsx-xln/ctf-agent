"""Tool signature tracking for loop detection + resource-failure stuckness."""

from __future__ import annotations

import json
import re
from collections import deque
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

    def check(self, tool_name: str, args: dict | str | None = None) -> str | None:
        """Check if the agent is stuck in a loop.

        Returns:
            None: no loop
            "warn": approaching loop threshold
            "break": exceeded loop threshold, should force-break
        """
        if args:
            raw = json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)
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
        """Track OOM/timeout-class failures across different commands.

        Returns \"oom_break\" when the agent keeps dying the same way.
        """
        cls = classify_resource_failure(result_text)
        if not cls:
            return None
        self._fail_classes.append(f"{tool_name}:{cls}")
        n = sum(1 for s in self._fail_classes if s.endswith(f":{cls}"))
        if n >= self.fail_break_threshold:
            return "oom_break" if cls in {"oom", "timeout"} else "fail_break"
        return None

    @property
    def last_sig(self) -> str:
        return self._recent[-1] if self._recent else ""

    def reset(self) -> None:
        self._recent.clear()
        self._fail_classes.clear()


def classify_resource_failure(text: str) -> str | None:
    t = (text or "").lower()
    if "[exit 137]" in t or "killed" in t or "out of memory" in t or "oom" in t:
        return "oom"
    if "[exit 124]" in t or "timed out" in t or "command timed out" in t:
        return "timeout"
    return None


LOOP_WARNING_MESSAGE = (
    "⚠️ **You are stuck in a loop** — you have run the exact same command multiple times "
    "with identical results. STOP repeating this command. Step back, reconsider your approach, "
    "and try a **completely different** technique or tool. "
    "If you were grepping/searching, try a Python script instead. "
    "If you were analyzing one aspect of the file, switch to another. "
    "What other angles haven't you explored?"
)

OOM_STUCK_MESSAGE = (
    "⚠️ **Resource failure loop** — recent commands died with OOM (exit 137) or timeout "
    "(exit 124). Do NOT retry the same heavy approach. "
    "Shrink the work, stream/chunk results, or use a lighter installed tool from "
    "`/challenge/TOOLS.txt`. Then switch strategy."
)

RESOURCE_HINTS = {
    137: (
        "Hint: exit 137 usually means the process was OOM-killed. "
        "Shrink memory use; prefer streaming / smaller steps; check /challenge/TOOLS.txt."
    ),
    124: (
        "Hint: exit 124 is a timeout. Narrow the search space or use a faster/lighter tool "
        "from /challenge/TOOLS.txt."
    ),
}
