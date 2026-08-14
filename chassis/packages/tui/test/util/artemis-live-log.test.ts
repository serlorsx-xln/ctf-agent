import { describe, expect, test } from "bun:test"
import {
  coalesceEvents,
  dedupeEvents,
  dropPostSolveAgentChatter,
  expandSummaryLine,
  hasAgentActivity,
  hasTerminalSolveOutcome,
  isGarbledModelText,
  isGlobalQuotaOutcome,
  isTerminalSolveOutcome,
  parseArtemisEvents,
  stripLogPrefix,
} from "../../src/util/artemis-live-log"

describe("parseArtemisEvents", () => {
  test("parses think lines", () => {
    const events = parseArtemisEvents("[chal/default think] Found a pcapng file.")
    expect(events).toEqual([{ kind: "think", agent: "default", text: "Found a pcapng file." }])
  })

  test("formats bash tool args as shell", () => {
    const events = parseArtemisEvents(
      '[chal/default tool#1 → bash] {"command": "capinfos /challenge/distfiles/capture.pcapng"}',
    )
    expect(events[0]?.kind).toBe("bash")
    if (events[0]?.kind === "bash") expect(events[0].command).toContain("capinfos")
  })

  test("formats INFO-prefixed tool lines (logging duplicate)", () => {
    const events = parseArtemisEvents(
      "19:52:47 INFO [tctt-junior-cipher-puzzle/default tool#4 -> bash] python3 << 'EOF'",
    )
    expect(events[0]?.kind).toBe("bash")
    if (events[0]?.kind === "bash") expect(events[0].command).toContain("python3")
  })

  test("drops Cursor SDK custom-user-tools dumps", () => {
    const events = parseArtemisEvents(
      '{"providerIdentifier":"custom-user-tools","toolName":"bash","args":{"command":"ls"}}',
    )
    expect(events).toEqual([])
  })

  test("drops python bytes noise", () => {
    expect(parseArtemisEvents("b'Hello friend...'")).toEqual([])
  })

  test("renames race exit footer", () => {
    const events = parseArtemisEvents("[race exit 0] (12 lines streamed)")
    expect(events.some((e) => e.kind === "outcome" && e.text.includes("[swarm exit"))).toBe(true)
  })

  test("formats plain bash command (solver live_log style)", () => {
    const events = parseArtemisEvents(
      "[chal/default tool#1 → bash] capinfos /challenge/distfiles/capture.pcapng",
    )
    expect(events[0]?.kind).toBe("bash")
    if (events[0]?.kind === "bash") expect(events[0].command).toContain("capinfos")
  })

  test("parses FLAG_CONFIRM for TUI dialog", () => {
    const events = parseArtemisEvents("[artemis] FLAG_CONFIRM id=abc123 flag=CTF{test}")
    expect(events).toContainEqual({ kind: "flag_confirm", id: "abc123", flag: "CTF{test}" })
  })

  test("parses FLAG_CONFIRM even with INFO prefix", () => {
    const events = parseArtemisEvents(
      "19:53:01 INFO [artemis] FLAG_CONFIRM id=deadbeef flag=flag{50fba860}",
    )
    expect(events).toContainEqual({ kind: "flag_confirm", id: "deadbeef", flag: "flag{50fba860}" })
  })

  test("parses FLAGS_ASK for digits dialog", () => {
    const events = parseArtemisEvents(
      "[artemis] FLAGS_ASK id=deadbeef default=2 challenge=tctt-junior-cipher-puzzle",
    )
    expect(events).toContainEqual({
      kind: "flags_ask",
      id: "deadbeef",
      default: 2,
      challenge: "tctt-junior-cipher-puzzle",
    })
  })

  test("parses boot lines for startup panel", () => {
    const events = parseArtemisEvents(
      "[artemis] boot L0 image=ctf-sandbox-core; packs=forensics\n[artemis] boot Starting Docker sandbox…",
    )
    expect(events.filter((e) => e.kind === "boot")).toHaveLength(2)
    expect(hasAgentActivity(events)).toBe(false)
  })

  test("boot lines from parallel solvers carry the owning agent", () => {
    const events = parseArtemisEvents(
      "[19:52:47] INFO     [tctt/cursor/grok-4.5] Starting Docker sandbox\n" +
        "[19:52:47] INFO     [tctt/cursor/composer-2.5] Starting Docker sandbox\n" +
        "[19:52:48] INFO     [tctt/cursor/grok-4.5] L0 image=ctf-sandbox-core",
    )
    expect(events.map((e) => (e.kind === "boot" ? e.text : e.kind))).toEqual([
      "grok-4.5 Starting Docker sandbox",
      "composer-2.5 Starting Docker sandbox",
      "grok-4.5 L0 image=ctf-sandbox-core",
    ])
    expect(events.every((e) => e.kind === "boot" && e.agent)).toBe(true)
  })

  test("untagged and reserved-tag boot lines stay agentless", () => {
    const events = parseArtemisEvents(
      "[artemis] boot L0 image=ctf-sandbox-core\n[INFO] Starting swarm",
    )
    expect(events.every((e) => e.kind === "boot" && !e.agent)).toBe(true)
  })

  test("parses the end-of-run recap and keeps its indentation", () => {
    const events = parseArtemisEvents(
      "[artemis] summary Solved by grok-4.5\n" +
        "[artemis] summary   xor'd the blob with the APK signature",
    )
    expect(events).toEqual([
      { kind: "summary", text: "Solved by grok-4.5" },
      { kind: "summary", text: "  xor'd the blob with the APK signature" },
    ])
  })

  test("expands What I tried / Why it worked section labels", () => {
    const pieces = expandSummaryLine(
      "Key insight: XOR key in the APK. What I tried: jadx on the wrapper. Why it worked: client-side PIN.",
    )
    expect(pieces.some((p) => /^Key insight/i.test(p))).toBe(true)
    expect(pieces.some((p) => /^What I tried/i.test(p))).toBe(true)
    expect(pieces.some((p) => /^Why it worked/i.test(p))).toBe(true)
  })

  test("expands jammed Solution summary numbered lists on ingest", () => {
    const pieces = expandSummaryLine(
      "FLAG: flag{x} **Solution summary:** 1. **DNS TXT** — mango 2. **TCP** — parts 3. **XOR** — done",
    )
    expect(pieces.some((p) => /^Solution summary/i.test(p))).toBe(true)
    expect(pieces.some((p) => p.startsWith("1. DNS"))).toBe(true)
    expect(pieces.some((p) => p.startsWith("2. TCP"))).toBe(true)
    expect(pieces.some((p) => p.startsWith("3. XOR"))).toBe(true)
    expect(pieces.every((p) => !p.includes("**"))).toBe(true)

    const events = parseArtemisEvents(
      "[artemis] summary FLAG: flag{x} **Solution summary:** 1. DNS key 2. TCP chat",
    )
    const texts = events.filter((e) => e.kind === "summary").map((e) => (e as { text: string }).text)
    expect(texts.some((t) => t.startsWith("1. DNS"))).toBe(true)
    expect(texts.some((t) => t.startsWith("2. TCP"))).toBe(true)
  })

  test("expands ## Solution Summary and does not split CIPHER_PART_1/2.", () => {
    const raw =
      "**FLAG: flag{50fba}** ## Solution Summary 1. **DNS** — mango 2. **TCP** — CIPHER_PART_1/2. 3. **ICMP** — decoy"
    const pieces = expandSummaryLine(raw)
    expect(pieces).toContain("Solution Summary")
    expect(pieces.some((p) => p.startsWith("2. TCP") && p.includes("CIPHER_PART_1/2."))).toBe(true)
    expect(pieces.some((p) => p === "2.")).toBe(false)
  })

  test("drops jammed FLAG/Solution Summary AI dumps (How owns recap)", () => {
    const raw =
      "[composer-2.5 ai] The flag was accepted. **FLAG: flag{x}** ## Solution Summary 1. DNS 2. TCP"
    expect(parseArtemisEvents(raw).filter((e) => e.kind === "ai")).toHaveLength(0)
  })

  test("drops short post-accept AI lines (recap owns the summary)", () => {
    const raw = "[composer-2.5 ai] The flag was accepted. I will output it on its own line."
    expect(parseArtemisEvents(raw).filter((e) => e.kind === "ai")).toHaveLength(0)
  })

  test("drops post-solve ai/think after CORRECT on the main feed", () => {
    const events = parseArtemisEvents(
      'CORRECT — accepted "flag{x}". Challenge complete for this run.\n' +
        "[composer-2.5 ai] The flag was accepted.\n" +
        "[composer-2.5 think] **XOR decryption** — step detail\n" +
        "[artemis] summary Solved by composer-2.5\n" +
        "[artemis] summary How:\n" +
        "[artemis] summary   Solution summary",
    )
    const trimmed = dropPostSolveAgentChatter(events)
    expect(trimmed.some((e) => e.kind === "ai")).toBe(false)
    expect(trimmed.some((e) => e.kind === "think")).toBe(false)
    expect(trimmed.some((e) => e.kind === "summary")).toBe(true)
  })

  test("drops token-streamed writeup deltas (summary owns the recap)", () => {
    expect(parseArtemisEvents("[chal/default writeup] How the flag")).toEqual([])
    expect(parseArtemisEvents("[chal/default writeup] was found")).toEqual([])
  })

  test("parses CORRECT as success outcome", () => {
    const events = parseArtemisEvents('CORRECT — accepted "flag{abc}". Challenge complete for this run.')
    expect(events[0]).toEqual({
      kind: "outcome",
      level: "success",
      text: 'CORRECT — accepted "flag{abc}". Challenge complete for this run.',
    })
    expect(hasAgentActivity(events)).toBe(true)
  })

  test("submit_flag result becomes outcome", () => {
    const events = parseArtemisEvents(
      "[chal/default tool#9 <- submit_flag] ACCEPTED \"flag{x}\" (1/1).",
    )
    expect(events.some((e) => e.kind === "outcome" && e.level === "success")).toBe(true)
  })

  test("parses [artemis] outcome CORRECT", () => {
    const events = parseArtemisEvents(
      '[artemis] outcome CORRECT — accepted "flag{abc}". Challenge complete for this run.',
    )
    expect(events[0]?.kind).toBe("outcome")
    expect(events[0]).toMatchObject({ level: "success" })
  })

  test("dedupes duplicate confirm / CORRECT lines", () => {
    const raw = [
      "[artemis] FLAG_CONFIRM id=abc flag=flag{x}",
      "[artemis] FLAG_CONFIRM id=abc flag=flag{x}",
      ">>> Confirmed — counting this flag.",
      ">>> Confirmed — counting this flag.",
      '[artemis] outcome CORRECT — accepted "flag{x}". Challenge complete for this run.',
      '[chal/default tool#9 <- submit_flag] CORRECT — accepted "flag{x}". Challenge complete for this run.',
    ].join("\n")
    const events = parseArtemisEvents(raw)
    expect(events.filter((e) => e.kind === "flag_confirm")).toHaveLength(1)
    expect(events.filter((e) => e.kind === "outcome" && /Confirmed/i.test(e.text))).toHaveLength(1)
    expect(events.filter((e) => e.kind === "outcome" && /^CORRECT/i.test(e.text))).toHaveLength(1)
  })

  test("dedupes Confirmed variants and drops FLAG FOUND after CORRECT", () => {
    const raw = [
      ">>> Confirmed — counting this flag.",
      ">>> Confirmed — counting this flag. (via cursor/composer-2.5)",
      '[artemis] outcome CORRECT — accepted "flag{x}" via cursor/composer-2.5. Challenge complete for this run.',
      "FLAG FOUND: flag{x} (solved by composer-2.5)",
    ].join("\n")
    const events = parseArtemisEvents(raw)
    expect(events.filter((e) => e.kind === "outcome" && /Confirmed/i.test(e.text))).toHaveLength(1)
    expect(events.filter((e) => e.kind === "outcome" && /^CORRECT/i.test(e.text))).toHaveLength(1)
    expect(events.filter((e) => e.kind === "outcome" && /^FLAG FOUND/i.test(e.text))).toHaveLength(0)
  })

  test("coalesces fragmented think tokens into one block", () => {
    // Real SDK deltas include their own spaces — do not invent extras.
    const merged = coalesceEvents([
      { kind: "think", agent: "default", text: "The" },
      { kind: "think", agent: "default", text: " structure" },
      { kind: "think", agent: "default", text: " is" },
      { kind: "think", agent: "default", text: " now" },
      { kind: "bash", agent: "default", command: "ls" },
    ])
    expect(merged).toHaveLength(2)
    expect(merged[0]).toEqual({ kind: "think", agent: "default", text: "The structure is now" })
    expect(merged[1]?.kind).toBe("bash")
  })

  test("does not insert spaces into flag / hex stream chunks", () => {
    const merged = coalesceEvents([
      { kind: "think", agent: "default", text: "fl" },
      { kind: "think", agent: "default", text: "ag{" },
      { kind: "think", agent: "default", text: "50" },
      { kind: "think", agent: "default", text: "fba860}" },
    ])
    expect(merged).toHaveLength(1)
    expect(merged[0]).toEqual({
      kind: "think",
      agent: "default",
      text: "flag{50fba860}",
    })
  })

  test("stripLogPrefix", () => {
    expect(stripLogPrefix("19:52:47 INFO [x] hi")).toBe("[x] hi")
  })

  test("usage-limit outcome is terminal for single-agent; multi only if no agent activity", () => {
    const events = parseArtemisEvents(
      "[artemis] outcome ERROR — Cursor usage limit reached — switch model or wait for reset (7/29/2026)",
    )
    expect(events[0]).toMatchObject({ kind: "outcome", level: "error" })
    expect(hasTerminalSolveOutcome(events, false)).toBe(true)
    // Global [artemis] account quota is always terminal (even multi soft-race).
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
    expect(isTerminalSolveOutcome(events[0]!, false)).toBe(true)
    expect(isTerminalSolveOutcome(events[0]!, true)).toBe(true)
  })

  test("global account quota is terminal even after agent think garbage", () => {
    const events = parseArtemisEvents(
      [
        "[chal/claude-fable-5 think] 嘶 暭 garbled",
        "[artemis] outcome ERROR — Cursor usage limit reached — switch model or wait for reset",
      ].join("\n"),
    )
    expect(hasAgentActivity(events)).toBe(true)
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
  })

  test("per-agent quota is not terminal in multi soft-race", () => {
    const events = parseArtemisEvents(
      [
        "[chal/default think] probing",
        "[chal/default] Cursor usage limit reached — switch model or wait for reset",
      ].join("\n"),
    )
    expect(events.some((e) => e.kind === "outcome" && e.agent === "default")).toBe(true)
    expect(hasTerminalSolveOutcome(events, true)).toBe(false)
    expect(hasTerminalSolveOutcome(events, false)).toBe(true)
  })

  test("global quota stays terminal independently of swarmRunning UI state", () => {
    // Contract: TUI unlocks Solving/chat on terminal outcome but may keep
    // swarmRunning true until swarm_exit — Esc/stop still use process-alive.
    const events = parseArtemisEvents(
      "[artemis] outcome ERROR — Cursor usage limit reached — switch model or wait for reset",
    )
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
    expect(isGlobalQuotaOutcome(events[0]!)).toBe(true)
  })

  test("raw usage-limit dump is humanized to terminal error", () => {
    const events = parseArtemisEvents(
      "You've hit your usage limit. Your limit resets on 7/29/2026. Upgrade to Ultra…",
    )
    expect(events[0]).toMatchObject({
      kind: "outcome",
      level: "error",
    })
    const text = events[0]?.kind === "outcome" ? events[0].text : ""
    expect(text).toMatch(/usage limit reached/i)
    expect(hasTerminalSolveOutcome(events, false)).toBe(true)
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
  })

  test("[status] usage-limit is global — no phantom agent field", () => {
    const events = parseArtemisEvents("[status] Cursor usage limit reached — switch model")
    expect(events[0]).toMatchObject({ kind: "outcome", level: "error" })
    expect(events[0]).not.toHaveProperty("agent")
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
  })

  test("swarm exit status is terminal", () => {
    const events = parseArtemisEvents("[swarm exit 0] (12 lines streamed)")
    expect(hasTerminalSolveOutcome(events)).toBe(true)
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
  })

  test("CORRECT is terminal even in multi-agent soft race", () => {
    const events = parseArtemisEvents(
      '[artemis] outcome CORRECT — accepted "flag{abc}". Challenge complete for this run.',
    )
    expect(hasTerminalSolveOutcome(events, true)).toBe(true)
  })

  test("mid-run ACCEPTED is not terminal", () => {
    const events = parseArtemisEvents('[artemis] outcome ACCEPTED "flag{x}" (1/2).')
    expect(events.some((e) => e.kind === "outcome" && e.level === "success")).toBe(true)
    expect(hasTerminalSolveOutcome(events)).toBe(false)
  })

  test("dedupes usage-limit outcomes that differ only by reset date", () => {
    const events = dedupeEvents([
      {
        kind: "outcome",
        level: "error",
        text: "Cursor usage limit reached — switch model or wait for reset (7/29/2026)",
      },
      {
        kind: "outcome",
        level: "error",
        text: "Cursor usage limit reached — switch model or wait for reset",
      },
    ])
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ text: expect.stringMatching(/7\/29\/2026/) })
  })

  test("drops garbled Cursor think/ai mojibake", () => {
    expect(isGarbledModelText("纯拔署疔鱷槽。")).toBe(true)
    expect(isGarbledModelText("وف و٧ؤ ١٢٢ئ٣٠ىغ ي٧ض٢ثي")).toBe(true)
    expect(isGarbledModelText("Looking at the APK entry points next")).toBe(false)
    const events = parseArtemisEvents(
      [
        "[chal/claude-fable-5 think] 纯拔署疔鱷槽。",
        "[chal/claude-fable-5 think] وف و٧ؤ ١٢٢ئ٣٠ىغ",
        "[artemis] outcome ERROR — Cursor usage limit reached — switch model or wait for reset (7/29/2026)",
      ].join("\n"),
    )
    expect(events.every((e) => e.kind !== "think")).toBe(true)
    expect(events.some((e) => e.kind === "outcome")).toBe(true)
  })

  test("drops garbled untagged and generic status lines", () => {
    const events = parseArtemisEvents(
      [
        "وف و٧ؤ ١٢٢ئ٣٠ىغ ي٧ض٢ثي هذه من",
        "[chal/claude-opus] وف و٧ؤ ١٢٢ئ٣٠ىغ ي٧ض٢ثي هذه من في",
        "[status] Waiting for operator",
      ].join("\n"),
    )
    expect(events.every((e) => !("text" in e && /وف/.test(String(e.text))))).toBe(true)
    expect(events.some((e) => e.kind === "status" && /Waiting for operator/.test(e.text))).toBe(true)
  })
})
