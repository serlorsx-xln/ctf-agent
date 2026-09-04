# Artemis system audit

วันที่: 4 กันยายน 2026  
ขอบเขต: checkout นี้ทั้งต้น (`backend/`, `chassis/`, `sandbox/`, `scripts/`, `tests/`) บวกหลักฐานจากการติดตั้งจริงบน Mac เครื่องนี้ (Docker Desktop 4.89, VM 8 GB, 18 CPU)  
วิธีทำ: อ่านโค้ดแบบ static แล้วยืนยันจุด critical/high ที่บรรทัดที่อ้าง — ไม่ใช่ pentest ของ swarm ที่กำลังรัน  
โมเดลภัยคุกคาม: **workstation คนเดียวบนเครื่อง local** ไม่ใช่ multi-tenant cloud

สรุปสั้น: ความเสี่ยงจากอินเทอร์เน็ตต่ำ (Unix socket `0o600` + TCP `127.0.0.1`) แต่บน LAN / โปรเซสเดียวกันมีช่องโหว่จริง สองจุดที่ควรแก้ก่อนคือ SOCKS5 เปิดที่ `0.0.0.0` โดยไม่มี auth และ Claude `WebFetch`/`WebSearch` ที่รันบนโฮสต์

ฉบับนี้เป็นรายงานหลักที่อ่านนอก IDE ได้ ใน Cursor มี canvas กรอง findings ได้อยู่ข้างแชทที่ทำ audit

| ความรุนแรง | จำนวน |
|---|---:|
| Critical | 2 |
| High | 10 |
| Medium | 8 |
| Low | 4 |
| **รวม** | **24** |

---

## 1. Threat model

| ผู้โจมตี | เข้าถึง daemon ได้หรือไม่ | ประเมิน |
|---|---|---|
| Remote ไม่มี foothold | ไม่ — socket จำกัด user, TCP จำกัด loopback | ต่ำ |
| เครื่องใน LAN เดียวกัน | ไม่ถึง daemon แต่ถึง SOCKS ถ้า bind `0.0.0.0` | สูง ตอนมี solve + VPN |
| โปรเซส UID เดียวกัน | ได้ — อ่าน `auth.json`, แอบเป็น TUI, ยืนยัน flag | สูง |
| ข้อความโจทย์ / distfiles | ไม่ได้รันบนโฮสต์โดยตรง แต่ฉีดเข้า prompt และรันใน container สิทธิ์สูง | สูงถ้าโจทย์ไม่น่าเชื่อถือ |

ชั้นที่ออกแบบมาให้แข็ง: แชทโฮสต์ห้าม `bash` / `read` / `webfetch` (`chassis/opencode.json`) การแก้โจทย์ต้องผ่าน daemon แล้วเข้า Docker

ชั้นที่เป็น toolbox ไม่ใช่ jail: container รัน root + `SYS_ADMIN` + `seccomp=unconfined` + egress เต็ม

---

## 2. แผนผังชั้น

```
Operator TUI (OpenCode)
  └─ chassis/artemis/plugin.ts  →  NDJSON unix socket
        └─ artemis-daemon
              ├─ SessionSlot ต่อหน้าต่าง TUI
              ├─ ChallengeSwarm (soft race, CORRECT ยกเลิกพี่น้อง)
              └─ DockerSandbox ต่อ solver
                    ├─ L0  ctf-sandbox-core
                    └─ L1  pack binds จาก ~/.cache/ctf-agent/packs
```

| ชั้น | สถานะ | หมายเหตุ |
|---|---|---|
| TUI chat | แข็ง | deny host tools; plugin ไป daemon |
| Daemon | เชื่อ local | multi-window แยกได้ถ้า client ซื่อสัตย์; ไม่มี auth |
| Swarm | soft race ที่คิดมาแล้ว | flag confirm กัน race ดี; winner Hold Q&A |
| Sandbox | workstation | ไม่ mount docker.sock เข้ากล่อง; root + cap กว้าง |

---

## 3. สิ่งที่แข็งอยู่แล้ว

- **TUI-first keys** — launcher โหลด `.env` แล้ว unset API key; กุญแจมาจาก `/connect` → `~/.local/share/artemis/auth.json`
- **Solve gate** — load ล้มไม่เปิด flags dialog
- **Flag confirm** — dialog ใน TUI ไม่ใช่ chat `y/N`; มี `_flag_lock`, inflight set, ไม่ให้พี่น้องแย่ง Hold (`tests/test_swarm_flag_confirm_race.py`)
- **Pack ops** — flock ตอน extract, digest + sentinel ตอน cache พัง, ล้าง AppleDouble สำหรับ ExFAT
- **Runtime OOM** — pack memory floor + loop detect มีเทส (คนละเรื่องกับ OOM ตอน **build** donor)
- **Install relocatable** — wrapper `~/.local/bin/artemis` + `install-path.txt`, code-stamp daemon, TUI binary cache + codesign บน macOS
- **Docker socket อยู่บนโฮสต์** — solver container คุม Docker ไม่ได้โดยตรง

---

## 4. รายการ findings

### Critical

#### C1 — SOCKS5 ไม่มี auth ฟังที่ `0.0.0.0`

- ความรุนแรง: critical · พื้นที่: Host network
- ที่อยู่: `backend/host_proxy.py:96` (greeting ที่บรรทัด 183)
- หลักฐาน: `asyncio.start_server(host="0.0.0.0")` แล้วตอบ SOCKS `\x05\x00` (ไม่ถามรหัส) CONNECT-only ใช้ routing table ของโฮสต์ รวม lab VPN
- ผลกระทบ: เครื่องใน LAN ใช้ Mac นี้เป็น relay เข้า HTB / เน็ตภายในได้ ตอนมี swarm รัน
- แก้: bind `127.0.0.1` หรือ IP ของ Docker bridge เท่านั้น; ใส่ shared secret; อย่า adopt listener เดิมตอน `EADDRINUSE`

#### C2 — Claude `WebFetch` / `WebSearch` รันบนโฮสต์

- ความรุนแรง: critical · พื้นที่: Claude solver
- ที่อยู่: `backend/agents/claude_solver.py:383` (`allowed_tools` ที่ 455–458)
- หลักฐาน: PreToolUse คืน `{}` ให้ WebFetch/WebSearch (อนุญาต) Read/Write/Glob ถูก deny; Bash ถูกส่งเข้า Docker; Cursor/Codex `web_fetch` มี `_is_internal_url()`
- ผลกระทบ: ข้อความโจทย์ชี้ Claude ไปดึง URL บนโฮสต์ / metadata / เน็ตภายใน นอก sandbox
- แก้: deny tool พวกนี้ แล้วส่งผ่าน `do_web_fetch` ใน sandbox พร้อม blocklist เดิม

### High

#### H1 — Container รัน root + `SYS_ADMIN` + `seccomp=unconfined`

- ที่อยู่: `backend/sandbox/container.py:194`
- หลักฐาน: `CapAdd: SYS_ADMIN, SYS_PTRACE`, `SecurityOpt: seccomp=unconfined`, `/dev/loop-control`, ไม่มี `USER` ใน Dockerfile
- ผลกระทบ: distfiles ที่เป็นอันตรายบวกช่องโหว่ kernel/Docker มีพื้นผิว breakout กว้าง — ตั้งใจเป็นกล่อง CTF ไม่ใช่ jail
- แก้: คง `SYS_PTRACE` เฉพาะงาน gdb; ตัด `SYS_ADMIN` และใช้ seccomp ปกติถ้า pack ไม่ต้องการ; rootfs อ่านอย่างเดียว + RW ที่ `/challenge/workspace`

#### H2 — Daemon ไม่มี auth; session/role ให้ client ประกาศเอง

- ที่อยู่: `backend/daemon/server.py:214` (`_sid` ที่ 256)
- หลักฐาน: `hello.role` / `hello.session` ใช้ตามที่ส่งมา `_sid()` ยึด `msg.session` มากกว่า binding ของ connection
- ผลกระทบ: โปรเซส UID เดียวกันสตาร์ท/หยุด swarm, กดรับ flag, ฉีด operator note, สั่ง setup ได้ Remote เข้า socket ไม่ได้
- แก้: ผูก session ตอน hello; ใส่ daemon token ไฟล์ `0o600`; เพิกเฉย `session` จาก peer บทบาท swarm/usage

#### H3 — API key อยู่ใน `auth.json` แบบ plaintext และใน environ

- ที่อยู่: `backend/shell/credentials.py:52`
- หลักฐาน: TUI เขียน `~/.local/share/artemis/auth.json` (`0o600`); `chassis/bin/artemis` `eval`-export กุญแจเข้าเชลล์; ลูก swarm สืบทอด; `/proc/<pid>/environ` อ่านได้ด้วย UID เดียวกัน
- ผลกระทบ: มัลแวร์หรือสคริปต์ npm/bun ที่โดนแฮกขโมยกุญแจ Cursor/Claude/OpenAI/Gemini ได้
- แก้: OS keychain; ส่งกุญแจผ่าน fd ไม่ใช่ `eval`; อย่าทิ้งกุญแจใน environ ของ TUI แม่

#### H4 — คำอธิบายโจทย์ถูกแปะใน prompt โดยไม่กัน

- ที่อยู่: `backend/prompts.py:150`
- หลักฐาน: `meta.description` อยู่ใต้ `## Description` ใน system prompt ทุกตัว; Claude writeup/QA ใส่ `description[:1500]` อีก
- ผลกระทบ: โจทย์แบบ adversarial สั่งให้ละเมิดกติกาเครื่องมือ, ส่ง flag ปลอม, หรือชวน operator; findings ของพี่น้องขยายข้อความเดิมผ่าน message bus
- แก้: ล้อม challenge / sibling / operator text ว่าเป็นข้อมูลที่ไม่น่าเชื่อถือ; ห้าม description ทับกฎ sandbox; ตัด description ดิบออกจาก writeup/QA

#### H5 — Load attachments ก็อป path บนโฮสต์เข้า sandbox ได้

- ที่อยู่: `backend/challenge.py:242`
- หลักฐาน: `resolve_load_target` + `_copy_attachments` ตาม symlink แล้วก็อปเข้า `distfiles` แล้ว bind เข้า Docker
- ผลกระทบ: โมเดลที่เรียก `artemis_load_challenge` ด้วย `path=/etc/…` ดึงไฟล์ที่อ่านได้บนโฮสต์เข้า workspace ได้
- แก้: allowlist ราก; ปฏิเสธ symlink ที่หนีออกนอกต้นทาง; ให้ operator ยืนยัน path นอก CWD

#### H6 — `write_file` ไม่จำกัด path

- ที่อยู่: `backend/sandbox/container.py:1505`
- หลักฐาน: `put_archive` เขียน path ใดก็ได้ในกล่อง รวมกับ root + `SYS_ADMIN`
- ผลกระทบ: output ของโมเดลทับไบนารีใน `/usr/local/bin` ที่รอบ ensure ถัดไปจะใช้
- แก้: จำกัดเขียนที่ `/challenge/workspace`; rootfs อ่านอย่างเดียวตอน solve

#### H7 — git clone ไม่ปัก SHA และ apt/pip ตอนรันด้วย root

- ที่อยู่: `sandbox/Dockerfile.crypto-tools:21`
- หลักฐาน: donor clone flatter / RsaCtfTool / cado-nfs / linpeas ตอน build; pack bootstrap รัน `apt-get`, `pip --break-system-packages`, `git+https` NetExec ในกล่อง solver
- ผลกระทบ: registry หรือแพ็กเกจ typosquat กลายเป็นโค้ด root ในทุก solve ที่ใช้ cache นั้น
- แก้: ปัก commit/hash; bake ตอน setup เท่านั้น; ปิด apt/pip ที่ agent เรียกได้ ยกเว้น allowlist

#### H8 — pack cache บนโฮสต์ถูกเชื่อและถูก execute

- ที่อยู่: `backend/sandbox/container.py:1042`
- หลักฐาน: bind RO จาก `~/.cache/ctf-agent/packs` เข้า `/opt`; `.ready` มี digest แต่ตอน bind ไม่ตรวจ hash รายไฟล์; cache เขียนได้ด้วย user
- ผลกระทบ: โปรเซส local แก้ `analyzeHeadless` หรือ `jadx` ติดทุก sandbox รอบหน้า
- แก้: ตรวจ digest ตอน bind; `chmod 0700` ที่ราก cache; ลายเซ็นถ้าต้องการ

#### H9 — donor build ใช้ `make -j$(nproc)` แล้ว OOM บน Docker 8 GB

- ที่อยู่: `sandbox/Dockerfile.crypto-tools:23`
- หลักฐานบนเครื่องนี้: `MemTotal=8GB`, `NCPU=18`; build แรกของ crypto-tools ตาย `ResourceExhausted: cannot allocate memory` ที่ cado-nfs; rebuild `-j2` ผ่าน
- ผลกระทบ: `artemis setup` พังบน Docker Desktop ค่าเริ่มต้นของ Mac; TUI ค้างที่ Install เพราะ pack `crypto-tools`
- แก้: จำกัดจ็อบ (`ARTEMIS_DONOR_BUILD_JOBS=2`); เอกสาร Docker RAM ≥ 12 GB; บอก OOM ให้ชัด

#### H10 — ข้อความโจทย์สั่ง probe RFC1918 / `.htb`

- ที่อยู่: `backend/sandbox/harden.py:11`
- หลักฐาน: parse โฮสต์แล้ว TCP probe จาก container เพื่อเลือก DIRECT กับ host SOCKS
- ผลกระทบ: โจทย์ที่พูดถึง `10.x` / `172.x` สแกน LAN หรือแล็บของ operator ได้
- แก้: ให้ operator ยืนยัน หรือ `--lab-hosts` ก่อน probe

### Medium

#### M1 — coordinator bump/trace ใช้ `model_spec` ไม่ใช่ `runner_id`

- ที่อยู่: `backend/agents/coordinator_core.py:210`
- หลักฐาน: `swarm.solvers` คีย์ด้วย `runner_id` (`cursor/x#2`); coordinator หาด้วย `model_spec`
- ผลกระทบ: swarm โมเดลซ้ำ bump / อ่าน trace ไม่ได้
- แก้: lookup `runner_id`; map ชื่อแสดงผ่าน `assign_runner_ids()`

#### M2 — งบ USD นับ cost ที่ขาดเป็น `$0`

- ที่อยู่: `backend/eval_run.py` (`EvalRunState.budget_exceeded`)
- หลักฐาน: Cursor/Gemini/Codex มักได้ `cost_usd=None`
- พฤติกรรมปัจจุบัน: ไม่เทียบ `None` เป็น `$0`. `--eval-max-usd` ตัดเมื่อ provider รายงานตัวเลข. ถ้า cost ไม่ทราบ — fail-open เฉพาะตอนมี `--eval-max-wall-s` คู่กัน (wall เป็น hard stop ของ unattended); USD-only + cost ไม่ทราบ = fail-closed เพื่อไม่ให้เพดานเป็น no-op
- ผลกระทบเดิม: รัน eval แบบเสียเงินไม่หยุด / หรือตัด Cursor ทันทีถ้าถือ `None` เป็นเกินงบ

#### M3 — `MemorySwap` เท่า `Memory`; live bump พังเงียบ

- ที่อยู่: `backend/sandbox/governor.py:44`
- หลักฐาน: `docker update --memory-swap` เท่า memory; `apply_live_memory` false คงลิมิตเก่า
- ผลกระทบ: พื้น crypto คือ 12g แต่ Docker Desktop เครื่องนี้ 8g — solve ได้ 137 ไม่มี swap เผื่อ
- แก้: preflight RAM ของ Docker กับพื้น pack; ถ้า bump ไม่ได้ให้ fail solve

#### M4 — setup gate ไม่ตรวจ DNS ใน container

- ที่อยู่: `backend/sandbox/setup_ready.py:106`
- หลักฐาน: L0 รอบแรกบน Mac นี้ pip พัง `Temporary failure in name resolution` ที่ pypi.org; `probe_setup_status` ดูแค่ `docker info` + image + `.ready`
- ผลกระทบ: ติดตั้งดูพร้อม แต่ bake แรกตายหลัง 5–15 นาที
- แก้: ใส่ `docker run … getent hosts pypi.org` / `archive.ubuntu.com` ใน setup gate

#### M5 — installer ไม่เขียน PATH และไม่ติดตั้ง Docker บน macOS

- ที่อยู่: `scripts/install.sh:212`
- หลักฐาน: export PATH แค่ในเชลล์ตอนติดตั้ง; `brew cask docker-desktop` ต้องการ sudo เพื่อลิงก์ `/usr/local/bin`; รอบนี้ก็อปแอปแล้วลิงก์ CLI ที่ `~/.local/bin`
- ผลกระทบ: เทอร์มินัลใหม่ไม่เห็น `artemis`/`docker`; install เริ่มต้นจบได้โดยไม่มี L0 ถ้า Docker ยังไม่ขึ้น
- แก้: ทำแบบ Windows path helper; แยก Docker.app กับ PATH; บอกเมื่อข้าม donor

#### M6 — daemon พังแล้ว plugin ถอยไป Python bridge เงียบๆ

- ที่อยู่: `chassis/artemis/plugin.ts:45`
- หลักฐาน: `daemonOp` spawn bridge พร้อม `process.env` ทั้งก้อนรวมกุญแจ และหลุด session ของ socket
- ผลกระทบ: ดีบักยาก; แยก session อ่อนกว่าทาง NDJSON
- แก้: fail ปิด หรือต้องมีแฟล็ก `unsafe-bridge` ชัดเจน

#### M7 — CLI `/msg` ที่ `127.0.0.1` ไม่มี auth

- ที่อยู่: `backend/agents/coordinator_loop.py:229`
- หลักฐาน: POST จาก local ฉีด coordinator inbox ได้ (คนละช่องกับ TUI operator inbox)
- ผลกระทบ: โปรเซส UID เดียวกันบังคับ batch coordinator ได้
- แก้: shared token หรือ Unix socket

#### M8 — Gemini ไม่มี `notify_coordinator` / `web_fetch` / `check_findings`

- ที่อยู่: `backend/agents/gemini_solver.py:36`
- หลักฐาน: `has_named_tools=False`; swarm ผสมให้ Gemini พื้นผิวแคบกว่าพี่น้อง
- ผลกระทบ: แข่งไม่แฟร์; แชร์ findings กลางทางไม่ได้
- แก้: เพิ่มเครื่องมือที่ขาด หรือเอกสารว่าตั้งใจให้เป็น subset

### Low

#### L1 — pack bootstrap ใช้ `|| true` กับ apt/pip

- ที่อยู่: `backend/tool_router.py:1308`
- ผลกระทบ: เขียน `.ready` ได้ทั้งที่ติดตั้งไม่ครบ แล้ว solve แรกค่อยเจอไบนารีหาย
- แก้: bootstrap ต้อง fail จริง; อย่าเขียน `.ready` ถ้าไม่ครบ

#### L2 — `docker commit` จาก sandbox ที่ยังมีชีวิต

- ที่อยู่: `backend/sandbox/warm_runtime.py:155`
- ผลกระทบ: ไฟล์ชั่วคราวใน `/root` ติด warm image
- แก้: commit จาก path ที่ scrub หรือจาก Dockerfile อย่างเดียว

#### L3 — TUI หลุด 10 วินาทีแล้ว auto-reject flag

- ที่อยู่: `backend/daemon/server.py:76`
- หลักฐาน: `TUI_RECONNECT_GRACE_S=10` แล้ว `cancel_pending_dialogs ok:false`
- ผลกระทบ: โน้ตบุ๊กหลับตอน confirm ทิ้ง flag จริงได้
- แก้: เขียนในเอกสาร; หรือพัก dialog ไว้จน reconnect

#### L4 — build TUI เขียน stub `chassis/.github/TEAM_MEMBERS`

- ที่อยู่: `scripts/build-tui.sh:15`
- ผลกระทบ: tree สกปรก ง่ายที่จะ commit โดยไม่ตั้งใจ
- แก้: gitignore stub หรือ vendor ไฟล์นั้น

---

## 5. ตารางรวม

| ID | Sev | พื้นที่ | ที่อยู่ | Finding |
|---|---|---|---|---|
| C1 | critical | Host network | `backend/host_proxy.py:96` | SOCKS5 ไม่มี auth ที่ `0.0.0.0` |
| C2 | critical | Claude solver | `backend/agents/claude_solver.py:383` | WebFetch/WebSearch บนโฮสต์ |
| H1 | high | Sandbox | `backend/sandbox/container.py:194` | root + SYS_ADMIN + seccomp=unconfined |
| H2 | high | Daemon | `backend/daemon/server.py:214` | ไม่มี auth; session ให้ client ตั้ง |
| H3 | high | Credentials | `backend/shell/credentials.py:52` | กุญแจ plaintext + environ |
| H4 | high | Prompts | `backend/prompts.py:150` | description ไม่ถูกกันใน prompt |
| H5 | high | Challenge load | `backend/challenge.py:242` | attachments ก็อป path โฮสต์ |
| H6 | high | Sandbox writes | `backend/sandbox/container.py:1505` | `write_file` ไม่จำกัด path |
| H7 | high | Supply chain | `sandbox/Dockerfile.crypto-tools:21` | git ไม่ปัก SHA; apt/pip ตอนรัน |
| H8 | high | Pack cache | `backend/sandbox/container.py:1042` | cache ถูกเชื่อและ execute |
| H9 | high | Install / ops | `sandbox/Dockerfile.crypto-tools:23` | `-j$(nproc)` OOM บน Docker 8 GB |
| H10 | high | Challenge net | `backend/sandbox/harden.py:11` | ข้อความโจทย์สั่ง probe RFC1918 |
| M1 | medium | Coordinator | `backend/agents/coordinator_core.py:210` | lookup `model_spec` ไม่ใช่ `runner_id` |
| M2 | medium | Eval | `backend/agents/swarm.py:583` | งบ USD นับ cost ว่างเป็น 0 |
| M3 | medium | Resources | `backend/sandbox/governor.py:44` | swap = memory; bump พังเงียบ |
| M4 | medium | DNS / setup | `backend/sandbox/setup_ready.py:106` | setup ไม่ตรวจ DNS ในกล่อง |
| M5 | medium | TUI / install | `scripts/install.sh:212` | ไม่เขียน PATH / ไม่ลง Docker บน Mac |
| M6 | medium | Plugin | `chassis/artemis/plugin.ts:45` | ถอยไป bridge เงียบ |
| M7 | medium | Coordinator HTTP | `backend/agents/coordinator_loop.py:229` | `/msg` ไม่มี auth |
| M8 | medium | Gemini | `backend/agents/gemini_solver.py:36` | เครื่องมือไม่ครบชุด |
| L1 | low | Bootstrap | `backend/tool_router.py:1308` | `\|\| true` ตอน apt/pip |
| L2 | low | Warm images | `backend/sandbox/warm_runtime.py:155` | commit sandbox มีชีวิต |
| L3 | low | Flags | `backend/daemon/server.py:76` | หลุด 10 วินาทีแล้ว reject flag |
| L4 | low | Build | `scripts/build-tui.sh:15` | เขียน TEAM_MEMBERS stub |

---

## 6. เทสที่มี vs ช่องว่าง

จำนวนด้านล่างเป็นประมาณการรวมต่อกลุ่มไฟล์ใน `tests/` ไม่ใช่ข้อเทสทีละเคส

| กลุ่ม | ประมาณจำนวนเทส | สถานะ |
|---|---:|---|
| Daemon protocol / transport / multi-session | 40 | แน่น — ยังไม่เทส auth ของ peer |
| Flag confirm / solve flow | 50 | แน่น |
| Launch / setup / warm | 25 | แน่น |
| Pack detect / donor metadata | 40 | แน่นเรื่อง metadata ไม่ใช่ build จริง |
| Credentials | 15 | พอ |
| Host SOCKS | 0 | ไม่มี |
| Claude WebFetch | 0 | ไม่มี |
| Donor OOM / `-j` | 0 | ไม่มี (OOM ตอนรันมีคนละชุด) |
| Coordinator E2E | 0 | เกือบไม่มี |

ไฟล์ตัวแทนที่แน่น: `test_daemon_flag_confirm.py`, `test_swarm_flag_confirm_race.py`, `test_daemon_solve_flow.py`, `test_launch_setup.py`, `test_install_path.py`, `test_credentials.py`, `test_governor.py`, `test_loop_detect.py`

---

## 7. หลักฐานติดตั้งบนเครื่องนี้

| ตรวจ | ผล | หมายเหตุ |
|---|---|---|
| uv / Bun / Python 3.14 / TUI binary | ผ่าน | `scripts/install.sh --skip-docker` |
| Docker Desktop 4.89 arm64 | ผ่าน | ก็อปจาก DMG; CLI ที่ `~/.local/bin` |
| pip ใน L0 รอบแรก (pwntools) | พังรอบเดียว | DNS ใน container ยังไม่พร้อม; ลองใหม่ผ่าน |
| donor `crypto-tools` `-j$(nproc)` | OOM | Docker 8 GB × 18 CPU; cado-nfs |
| donor `crypto-tools` `-j2` | ผ่าน | อิมเมจ 568 MB; pack cache พร้อม |
| RAM ของ Docker กับพื้น crypto | ไม่ตรง | VM 8g, พื้น pack 12g |

โฮสต์มี RAM 24 GB — ควรตั้ง Docker Desktop เป็นอย่างน้อย 12 GB ก่อน solve หมวด crypto

---

## 8. ลำดับแก้ที่แนะนำ

1. **C1** — bind SOCKS ที่ loopback และใส่ auth นี่คือจุดเดียวที่โจมตีจาก LAN ได้โดยไม่ต้องรันโค้ดบนเครื่อง
2. **C2** — deny Claude WebFetch/WebSearch บนโฮสต์ แล้วใส่ regression test
3. **H4** — ล้อมข้อความโจทย์ / พี่น้อง / operator ว่าไม่น่าเชื่อถือ
4. **H9 + M4** — จำกัดจ็อบตอน build donor และตรวจ DNS ใน setup gate; เพิ่ม RAM ของ Docker บนเครื่องนี้เป็น 12 GB
5. **H2 + M1** — daemon hello token + ผูก session; coordinator หาด้วย `runner_id`
6. **M5 + H7** — เขียน PATH ลงโปรไฟล์เชลล์; ปัก SHA ของ git ใน donor งานถัดไปที่ทำได้: non-root sandbox และ allowlist ของ `write_file`

---

## 9. สิ่งที่รายงานนี้ไม่ได้ทำ

- ไม่รัน swarm จริงกับโจทย์ adversarial
- ไม่เปิด `auth.json` หรือไฟล์ตั้งค่า Docker Desktop ทั้งก้อน
- ไม่ audit ต้นน้ำ OpenCode ทั้ง tree นอกส่วนที่ Artemis ต่อ
- ไม่รับประกันว่าไม่มีช่องโหว่ที่ยังไม่ได้อ่าน

อ้างอิงสถาปัตย์ที่ล็อกไว้: [ARCHITECTURE-SANDBOX.md](./ARCHITECTURE-SANDBOX.md), [TUI-PRODUCT-FLOW.md](./TUI-PRODUCT-FLOW.md)

---

## 10. สถานะ remediation (4 กันยายน 2026)

ทำแล้วใน checkout นี้:

| ID | สถานะ | หมายเหตุ |
|---|---|---|
| C1 | แก้แล้ว | bind ไม่ใช่ `0.0.0.0`; SOCKS5 user/pass; ไม่ adopt listener |
| C2 | แก้แล้ว | deny `WebFetch`/`WebSearch`; MCP `web_fetch` + RFC1918 block |
| H4 | แก้แล้ว | `fence_untrusted()` ล้อม description / operator notes |
| H9 | แก้แล้ว | `ARG BUILD_JOBS=2` + `ARTEMIS_DONOR_BUILD_JOBS` |
| M4 | แก้แล้ว | `getent hosts pypi.org` เป็น advisory (`dns_ok`) ไม่บล็อก ready |
| H2 | แก้แล้ว | `ARTEMIS_DAEMON_TOKEN` + ไฟล์ `0o600`; swarm/usage ไม่สลับ session ต่อข้อความ |
| M1 | แก้แล้ว | coordinator หาด้วย `runner_id` แล้วค่อย `model_spec` ที่ไม่คลุมเครือ |
| H5 | แก้แล้ว | allowlist CWD / cache / `challenges/` / temp / `ARTEMIS_LOAD_ROOTS` |
| H6 | แก้แล้ว | `write_file` จำกัดที่ `/challenge/workspace` |
| H10 | แก้แล้ว | RFC1918 จากข้อความต้อง `CTF_ALLOW_LAB_PROBE=1` หรือ `CTF_LAB_HOSTS`; `*.htb`/`nc` ผ่าน |
| M2 | แก้แล้ว | USD นับเฉพาะ cost ที่รายงาน; `None` fail-open เฉพาะเมื่อมี wall คู่กัน |
| M5 | แก้แล้ว | `install.sh` เขียน `~/.local/bin` ลง `.zprofile` / `.zshrc` |
| M6 | แก้แล้ว | plugin ถอยไป bridge เฉพาะ `ARTEMIS_UNSAFE_BRIDGE=1` |
| M7 | แก้แล้ว | `/msg` ตรวจ token เมื่อตั้ง `ARTEMIS_MSG_TOKEN` |
| L4 | แก้แล้ว | gitignore `chassis/.github/TEAM_MEMBERS` |

ยังไม่ทำ (ตั้งใจข้ามหรืองานใหญ่):

| ID | เหตุผล |
|---|---|
| H1 | ตัด `SYS_ADMIN` / seccomp จะพัง loop/qemu box |
| H3 | OS keychain / เลิก `eval` กุญแจ — งานใหญ่ |
| H7 | ปัก git SHA ตอนนี้ยังไม่มี commit ที่ล็อกไว้; ใช้ H9 กัน OOM |
| H8 | digest รายไฟล์ตอน bind |
| M3 | MemorySwap / fail ตอน bump ไม่ได้ |
| M8 | Gemini tool parity |
| L1–L3 | `\|\| true`, warm commit, flag auto-reject 10s |
