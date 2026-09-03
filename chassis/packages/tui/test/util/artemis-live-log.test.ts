import { describe, expect, test } from "bun:test"
import {
  coalesceEvents,
  dedupeEvents,
  dropPostSolveAgentChatter,
  eventsFromLogLine,
  expandSummaryLine,
  hasAgentActivity,
  hasCorrectSolveOutcome,
  hasTerminalSolveOutcome,
  isFeedVisibleStatus,
  isGarbledModelText,
  isGlobalQuotaOutcome,
  isSolverHoldActive,
  isTerminalSolveOutcome,
  parseArtemisEvents,
  pendingOperatorQueue,
  stripLogPrefix,
  type ArtemisEvent,
} from "../../src/util/artemis-live-log"

describe("parseArtemisEvents", () => {
  test("parses think lines", () => {
    const events = parseArtemisEvents("[chal/default think] Found a pcapng file.")
    expect(events).toEqual([{ kind: "think", agent: "default", text: "Found a pcapng file." }])
  })

  test("keeps slashy Claude model ids as the agent key", () => {
    const events = parseArtemisEvents("[ch05_7.83hz/aliyuncs/glm-5.2 think] counting letters")
    expect(events).toEqual([
      { kind: "think", agent: "aliyuncs/glm-5.2", text: "counting letters" },
    ])
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

  test("expandSummaryLine shared fixtures match backend expand_summary_line", async () => {
    const path = new URL("../../../../../tests/fixtures/summary_expand_cases.json", import.meta.url)
    const cases = (await Bun.file(path).json()) as Array<{
      id: string
      input: string
      expect_exact?: string[]
      expect_contains?: string[]
    }>
    for (const c of cases) {
      const pieces = expandSummaryLine(c.input)
      if (c.expect_exact) expect(pieces).toEqual(c.expect_exact)
      for (const needle of c.expect_contains ?? []) {
        expect(pieces.some((p) => p.includes(needle))).toBe(true)
      }
    }
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

  test("keeps Hold Q&A status replies after CORRECT (drops post-solve tools)", () => {
    const events = parseArtemisEvents(
      [
        'CORRECT — accepted "flag{x}". Challenge complete for this run.',
        "[composer-2.5 ai] late chatter before hold",
        "[default bash] cat notes.txt",
        "[default result] wizard_1 : b'[MAYA FOREST...'",
        "[artemis] hold — ask follow-ups",
        "[artemis] you (steer→default): คุณทำได้ยัง",
        "[artemis] qa-wait · คุณทำได้ยัง",
        "[artemis] qa →default: Yes — three flags are accepted.",
      ].join("\n"),
    )
    const trimmed = dropPostSolveAgentChatter(events)
    expect(trimmed.some((e) => e.kind === "ai" && /late chatter/.test(e.text))).toBe(false)
    expect(trimmed.some((e) => e.kind === "bash" || e.kind === "result")).toBe(false)
    expect(trimmed.some((e) => e.kind === "status" && /Q&A · Yes — three flags/.test(e.text))).toBe(
      true,
    )
    expect(
      trimmed.some(
        (e) => e.kind === "status" && e.agent === "default" && /Q&A · Yes/.test(e.text),
      ),
    ).toBe(true)
    expect(trimmed.some((e) => e.kind === "operator")).toBe(true)
    expect(trimmed.some((e) => e.kind === "status" && /^Answering/.test(e.text))).toBe(true)
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

  test("dedupeEvents prior seed suppresses FLAG FOUND outside the merge window", () => {
    const prior: ArtemisEvent[] = [
      {
        kind: "outcome",
        level: "success",
        text: 'CORRECT — accepted "flag{x}". Challenge complete for this run.',
      },
    ]
    const tail = dedupeEvents(
      [{ kind: "outcome", level: "success", text: "FLAG FOUND: flag{x}" }],
      prior,
    )
    expect(tail).toHaveLength(0)
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

  test("global Cursor usage-limit outcome is always terminal (single and multi)", () => {
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

  test("parses operator you (steer|queue) lines", () => {
    const events = parseArtemisEvents(
      ["[artemis] you (steer): try XSS", "[artemis] you (queue): later"].join("\n"),
    )
    expect(events).toContainEqual({ kind: "operator", text: "try XSS", delivery: "steer" })
    expect(events).toContainEqual({ kind: "operator", text: "later", delivery: "queue" })
    expect(hasAgentActivity(events)).toBe(true)
  })

  test("parses operator target scopes", () => {
    const events = parseArtemisEvents(
      [
        "[artemis] you (steer→all): hi",
        "[artemis] you (queue→default#2): only two",
        "[artemis] solver ← operator (queue→default#2): only two",
      ].join("\n"),
    )
    expect(events).toContainEqual({ kind: "operator", text: "hi", delivery: "steer" })
    expect(events).toContainEqual({
      kind: "operator",
      text: "only two",
      delivery: "queue",
      target: "default#2",
    })
    expect(pendingOperatorQueue(events)).toEqual([])
    expect(pendingOperatorQueue(events, "default#1")).toEqual([])
  })

  test("pendingOperatorQueue tracks queue until Solver read", () => {
    const queued = parseArtemisEvents(
      ["[artemis] you (queue): ทดสอบ", "[artemis] you (queue): ทดสอบ2"].join("\n"),
    )
    expect(pendingOperatorQueue(queued)).toEqual(["ทดสอบ", "ทดสอบ2"])
    const afterSteer = parseArtemisEvents(
      [
        "[artemis] you (queue): ทดสอบ",
        "[artemis] you (steer): interrupt",
        "[artemis] solver ← operator (steer): interrupt",
      ].join("\n"),
    )
    expect(pendingOperatorQueue(afterSteer)).toEqual(["ทดสอบ"])
    const sameTextSteer = parseArtemisEvents(
      [
        "[artemis] you (queue): สวัสดี",
        "[artemis] you (steer): สวัสดี",
        "[artemis] solver ← operator (steer): สวัสดี",
      ].join("\n"),
    )
    expect(pendingOperatorQueue(sameTextSteer)).toEqual(["สวัสดี"])
    const drained = parseArtemisEvents(
      [
        "[artemis] you (queue): ทดสอบ",
        "[artemis] you (queue): ทดสอบ2",
        "[artemis] solver ← operator (queue): ทดสอบ",
        "[artemis] solver ← operator (queue): ทดสอบ2",
      ].join("\n"),
    )
    expect(pendingOperatorQueue(drained)).toEqual([])
  })

  test("pendingOperatorQueue scopes by agent focus", () => {
    const events = parseArtemisEvents(
      [
        "[artemis] you (queue→default#1): a",
        "[artemis] you (queue→default#2): b",
      ].join("\n"),
    )
    expect(pendingOperatorQueue(events, "default#1")).toEqual(["a"])
    expect(pendingOperatorQueue(events, "default#2")).toEqual(["b"])
    expect(pendingOperatorQueue(events)).toEqual(["a", "b"])
  })

  test("pendingOperatorQueue matches base↔#N focus like backend targets_match", () => {
    const events = parseArtemisEvents("[artemis] you (queue→opus): note for opus family")
    expect(pendingOperatorQueue(events, "opus#1")).toEqual(["note for opus family"])
    expect(pendingOperatorQueue(events, "composer#1")).toEqual([])
  })

  test("pendingOperatorQueue unscoped Solver read does not clear scoped note", () => {
    const note = "x".repeat(40)
    const events = parseArtemisEvents(
      [
        `[artemis] you (queue→opus#2): ${note}`,
        `[artemis] solver ← operator (queue): ${note}`,
      ].join("\n"),
    )
    expect(pendingOperatorQueue(events)).toEqual([note])
    expect(pendingOperatorQueue(events, "opus#2")).toEqual([note])
  })

  test("pendingOperatorQueue matches truncated Solver read prefix", () => {
    const long = "x".repeat(800)
    const events = parseArtemisEvents(
      [`[artemis] you (queue): ${long}`, `[artemis] solver ← operator (queue): ${long.slice(0, 500)}`].join(
        "\n",
      ),
    )
    expect(pendingOperatorQueue(events)).toEqual([])
  })

  test("pendingOperatorQueue does not clear on short unrelated Solver read", () => {
    const events = parseArtemisEvents(
      [
        "[artemis] you (queue): please check the /admin path carefully",
        "[artemis] solver ← operator (queue): please",
      ].join("\n"),
    )
    expect(pendingOperatorQueue(events)).toEqual(["please check the /admin path carefully"])
  })

  test("parses Claude Bash and Cursor host Shell without step", () => {
    expect(
      parseArtemisEvents('[chal/opus tool#3 → Bash] {"command": "ls /challenge"}'),
    ).toContainEqual({
      kind: "bash",
      agent: "opus",
      command: "ls /challenge",
    })
    expect(
      parseArtemisEvents("[chal/default tool → Shell] ls -la /tmp"),
    ).toContainEqual({
      kind: "bash",
      agent: "default",
      command: "ls -la /tmp",
    })
    expect(
      parseArtemisEvents("[chal/default tool ← Shell (completed)] ok done"),
    ).toContainEqual({
      kind: "result",
      agent: "default",
      text: "ok done",
    })
  })

  test("qa-wait inserts qa_sep turn breaks between Hold replies", () => {
    const events = parseArtemisEvents(
      [
        "[artemis] qa-wait · q1",
        "[artemis] qa →default: first reply",
        "[artemis] qa-done",
        "[artemis] qa-wait · q2",
        "[artemis] qa →default: second reply",
        "[artemis] qa-done",
      ].join("\n"),
    )
    expect(events.filter((e) => e.kind === "qa_sep")).toHaveLength(4)
    const qa = events.filter((e) => e.kind === "status" && /^Q&A ·/.test(e.text))
    expect(qa.map((e) => (e.kind === "status" ? e.text : ""))).toEqual([
      "Q&A · first reply",
      "Q&A · second reply",
    ])
  })

  test("qa-done inserts qa_sep without a feed-visible status", () => {
    const events = parseArtemisEvents("[artemis] qa-done")
    expect(events.some((e) => e.kind === "qa_sep")).toBe(true)
    expect(events.some((e) => e.kind === "status" && /^Q&A done/i.test(e.text))).toBe(false)
  })

  test("eventsFromLogLine mirrors parseArtemisEvents qa_sep for live ingest", () => {
    const pieces = eventsFromLogLine("[artemis] qa-wait · next")
    expect(pieces.some((e) => e.kind === "qa_sep")).toBe(true)
    expect(pieces.some((e) => e.kind === "status" && /^Answering/.test(e.text))).toBe(true)
  })

  test("preserves Q&A body whitespace for markdown paragraphs", () => {
    const events = parseArtemisEvents(
      "[artemis] qa →default: line one\n[artemis] qa →default: \n[artemis] qa →default:   indented",
    )
    const qa = events.filter((e) => e.kind === "status" && /^Q&A/.test(e.text)) as Array<{
      kind: "status"
      text: string
    }>
    expect(qa.map((e) => e.text)).toEqual([
      "Q&A · line one",
      "Q&A · ",
      "Q&A ·   indented",
    ])
  })

  test("parses hold qa-wait and qa replies (Answering is footer-only, not feed)", () => {
    const events = parseArtemisEvents(
      [
        "[artemis] qa-wait · คุณทำได้ยัง",
        "[artemis] qa →default: Yes — three flags are accepted.",
        "[artemis] qa legacy untagged reply",
      ].join("\n"),
    )
    expect(events).toContainEqual({
      kind: "status",
      text: "Answering… · คุณทำได้ยัง",
    })
    expect(events).toContainEqual({
      kind: "status",
      text: "Q&A · Yes — three flags are accepted.",
      agent: "default",
    })
    expect(events).toContainEqual({
      kind: "status",
      text: "Q&A · legacy untagged reply",
    })
    expect(isFeedVisibleStatus("Answering… · คุณทำได้ยัง")).toBe(false)
    expect(isFeedVisibleStatus("Follow-up · interrupting default")).toBe(false)
    expect(isFeedVisibleStatus("Solver read · hi")).toBe(false)
    expect(isFeedVisibleStatus("Q&A · Yes — three flags are accepted.")).toBe(true)
    expect(isFeedVisibleStatus("Hold — ask follow-ups")).toBe(true)
  })

  test("isSolverHoldActive tracks hold then release", () => {
    const held = parseArtemisEvents(
      "[artemis] outcome CORRECT — done\n[artemis] hold — ask follow-ups",
    )
    expect(isSolverHoldActive(held)).toBe(true)
    expect(hasCorrectSolveOutcome(held)).toBe(true)
    const released = parseArtemisEvents(
      "[artemis] hold — ask follow-ups\n[artemis] hold released",
    )
    expect(isSolverHoldActive(released)).toBe(false)
    expect(released.some((e) => e.kind === "status" && e.text === "Hold released")).toBe(true)
  })
})
