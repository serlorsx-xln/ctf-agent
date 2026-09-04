# Sandbox Architecture — Lazy Packs (L0 + L1)

เอกสารนี้อธิบายสถาปัตย์ที่เลือกไว้สำหรับ **Artemis** (fork จาก Veria CTF Agent / Cursor backend)
เพื่อให้ **เบาบนเครื่องลูกค้า** แต่ **ทำโจทย์ได้ครบหมวด** โดยไม่เปลี่ยน logic หลักของระบบ

สถานะ: **สถาปัตย์ล็อกแล้ว** — Phase 0–3 (+ warm runtime) ลงโค้ดแล้ว  
อัปเดตล่าสุด: 2026-08-30  

| Phase | สถานะ |
|-------|--------|
| 0 local flag accept / tools.txt | ทำแล้ว |
| 1 L0 `Dockerfile.core` + additive `ensure_pack` + host cache + prefetch | ทำแล้ว |
| 2 packs (mobile / pwn / crypto / crypto-tools / steg / linux / forensics / web / ml / containers) | ทำแล้ว — ครอบคลุมชุดต้นฉบับ + linux box เบา |
| 3 Customer CLI setup / bake | ทำแล้ว — ``artemis setup`` (L0 + common packs; blutter VM shared) |
| 3b Warm runtime commit | ทำแล้ว — ``ctf-sandbox-warm-<pack>`` หลัง setup สำหรับ pack ที่ apt/pip หนัก |
| 3c Donor L0 fast finalize | ทำแล้ว — เมื่อ L0 คือ ``ctf-sandbox-mobile`` / ``pwn`` / warm-* ข้าม apt/pip ตอน prefetch (เหลือแค่ wrapper/symlink/marker) |

**ยกเลิกแล้ว (ไม่ทำ):** L2 Customer Kali bridge · L3 cloud rental worker  
เหตุผล: ไม่คุ้ม / ไม่ตรงผลิตภัณฑ์ — ลูกค้าใช้ L0+L1 บนเครื่องตัวเอง; แผนไกลเรื่องขาย = **brain บนเซิร์ฟเวอร์เรา + tools บนเครื่องลูกค้า** (deferred SaaS) ไม่ใช่ bridge ไป Kali หรือย้าย sandbox ขึ้นคลาวด์

โค้ดหลัก: `backend/tool_router.py`, `backend/sandbox/` (`ensure_pack`, governor, packs), `sandbox/Dockerfile.core`  
Cache: `~/.cache/ctf-agent/packs/` (หรือ `CTF_PACK_CACHE`)

นี่คือทางที่ **ดีกว่านี้ไม่ได้ภายใต้ข้อจำกัดผลิตภัณฑ์ปัจจุบัน**  
(agent logic เดิม · local-first · เบาบนเครื่องลูกค้า · ครบหมวดผ่าน lazy packs)

---

## 1. เป้าหมายและข้อจำกัด

### 1.1 เป้าหมายผลิตภัณฑ์

1. Agent แก้โจทย์ CTF ได้หลายหมวด: crypto, pwn, web, rev, mobile, forensics, misc
2. เครื่องลูกค้าต้อง **เบา** — ไม่บังคับโหลด Kali/tools เต็ม (~10–20GB+) ทุกคน
3. ความสามารถครบผ่าน **L0 + L1 lazy packs** บนเครื่องลูกค้า (ไม่พึ่ง Kali ของลูกค้า)
4. แผนไกล (hosted brain / SaaS) แยก brain/hands — deferred; ไม่ใช่ชั้น sandbox L2/L3
5. **ไม่เปลี่ยน logic ระบบหลัก** ที่ใช้อยู่ตอนนี้

### 1.2 สิ่งที่ระบบปัจจุบันเป็นอยู่ (ต้องคงไว้)

```
artemis swarm / coordinator
  → สร้าง Docker sandbox ใบเดียวต่อ challenge (หรือ shared ตาม config)
  → agent เรียก tools: bash, read_file, write_file, list_files, submit_flag, …
  → คำสั่งรันใน sandbox
  → ได้ flag (local accept → FLAG_FOUND)
```

จุดสำคัญที่ **ห้ามทำลาย**:

- Agent ยังคิดว่ามี **sandbox เดียว** และพูดกับมันผ่าน bash/MCP เหมือนเดิม
- ไม่บังคับให้ agent เลือก image หลายใบตอน runtime (`--image crypto` / `--image pwn` สลับไปมา)
- Flow `artemis swarm --challenge …` / multi-challenge หลัง setup แล้วยังใช้คำสั่งเดิมได้

### 1.3 ปัญหาที่เจอจากการทดลองจริง

| ปัญหา | ตัวอย่าง |
|--------|----------|
| Image เบาเกินไป | `ctf-sandbox` (sage) ไม่มี blutter / r2 พอ → PWNKnight วน strings |
| Image จัดเต็ม | หนักเครื่อง, build นาน, RAM 4GB OOM, Mac ARM + Colima อึดอัด |
| Tools ผิดหมวด | `ctf-sandbox-pwn` ช่วย binary pwn แต่ไม่แก้ Flutter AOT โดยตรง |
| Flag decoys | `submit_flag` ต้อง reject placeholder / fake_flag |

บทเรียน: **ยัดครบใน image เดียวบนเครื่องลูกค้า = ไม่ไหว**  
แต่ **เบาอย่างเดียวโดยไม่มีทางขยาย = ทำโจทย์ยากไม่ได้**

---

## 2. สถาปัตย์ที่เลือก (สรุปหนึ่งบรรทัด)

> **หน้าตาเดิม (sandbox ใบเดียว + bash) — ข้างหลังฉลาด: แกนเบา + ดึง tool packs เฉพาะตอนต้องใช้**

ภายใต้ข้อจำกัดของผลิตภัณฑ์นี้ นี่คือทางสถาปัตย์ที่ดีที่สุดแล้ว  
รายละเอียด implement (cache, prefetch) ปรับได้โดยไม่ต้องเปลี่ยนโครงใหญ่

---

## 3. ภาพรวมชั้น (L0–L1)

```
┌─────────────────────────────────────────────────────────────┐
│  Agent / Coordinator  (logic เดิม)                          │
│  bash / read_file / write_file / submit_flag                │
└────────────────────────────┬────────────────────────────────┘
                             │ API เดิม: “sandbox เดียว”
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  Sandbox Runtime Router  (โปร่งใสต่อ agent)                  │
│  - รับคำสั่ง bash                                            │
│  - ตรวจว่าต้องใช้ pack ไหน                                   │
│  - ensure tools พร้อม แล้วค่อยรัน                            │
└───────┬─────────────────┬───────────────────────────────────┘
        │                 │
        ▼                 ▼
   ┌────────┐       ┌──────────┐
   │  L0    │       │  L1      │
   │ Core   │◄─────►│ Lazy     │
   │ เบา    │ mount │ Packs    │
   └────────┘       └──────────┘
```

| ชั้น | ชื่อ | บทบาท | เมื่อไหร่ |
|------|------|--------|----------|
| **L0** | Core sandbox | รันตลอด เบา พอเริ่มโจทย์ | ทุกลูกค้า ทุกโจทย์ |
| **L1** | Lazy tool packs | ดึงชุด tools ตามความต้องการ | เจอไฟล์/หมวดที่ต้องใช้ |

Agent **ไม่ต้องรู้** ว่าคำสั่งไป L0 หรือ L1 — เห็นแค่ผล bash สำเร็จ/ล้มเหลว

---

## 4. รายละเอียดแต่ละชั้น

### 4.1 L0 — Core (ทุกเครื่อง, เปิดตลอด)

**เป้าหมายขนาด:** ประมาณ 500MB–1.5GB (compressed image ยิ่งดีถ้าต่ำกว่า 1GB)

**ต้องมีอย่างน้อย:**

- OS บาง (Ubuntu minimal หรือ distroless-friendly base)
- `bash`, `curl`, `wget`, `git`, `ca-certificates`
- `python3` + `pip` + `pwntools` (minimal)
- `gdb` พื้นฐาน, `binutils` (`strings`, `objdump`, `readelf`)
- `file`, `xxd`, `netcat`, `socat`
- ไฟล์ `/tools.txt` อธิบายว่า “มีแกนอะไร + packs ที่ดึงเพิ่มได้ยังไง”

**ไม่ใส่ใน L0:**

- SageMath ทั้งก้อน
- Chromium / Playwright เต็มชุด
- Ghidra
- blutter + Dart SDK
- Kali / mega-metapackages ทั้งก้อน

**ทำไมต้องเบา:**  
เปิด sandbox หลายโจทย์พร้อมกันได้, Mac + Colima อยู่ได้, onboarding ไม่รอนานชั่วโมง

---

### 4.2 L1 — Lazy Tool Packs (หัวใจของ “เบาแต่ครบ”)

#### แนวคิด

- ไม่ติดตั้ง tools ครบตอน build แรก
- เมื่อระบบ (หรือ agent ทางอ้อม) ต้องการ tool ที่ไม่มี → **ดึง pack** จาก registry/cache เข้า container เดิม
- ครั้งถัดไปใช้ **hot cache บนดิสก์** → เร็ว

#### Pack ที่ควรมี (เริ่มต้น)

| Pack ID | หมวด | เนื้อหาหลัก (ตัวอย่าง) |
|---------|------|-------------------------|
| `pwn` | binary exploitation | qemu-user-static, gef, one_gadget, patchelf, seccomp-tools, ROPgadget, r2 |
| `crypto` | cryptography | SageMath donor + L0 crypto helpers |
| `web` | web | nmap, sqlmap, flask, PyJWT, nodejs/wabt (apt/pip on L0 — no heavy browser stack) |
| `ghidra` | reverse engineering | Ghidra headless / PyGhidra donor |
| `mobile` | Android/iOS CTF | jadx, apktool, blutter, frida-tools |
| `steg` | steganography / media | steghide, stegseek donor, zsteg, exiftool, tesseract |
| `forensics` | forensics / DFIR | binwalk, sleuthkit, volatility3, tshark, scapy (apt/pip on L0) |
| `linux` | Linux AD / box helpers | ffuf, katana, linpeas, impacket, NetExec, … |
| `osint` | misc/osint | **not implemented** — use `steg`/`web` + host `curl`; optional future pack |

Pack แยกได้ละเอียดกว่านี้ในอนาคต (เช่น `mobile-flutter` แยกจาก `mobile-java`) แต่ตอนแรกชุดด้านบนพอ

#### รูปแบบเก็บ pack (ทางเลือก implement — เรียงจากแนะนำ)

1. **Docker layer / OCI artifact ต่อ pack**  
   - `ghcr.io/…/ctf-pack-mobile:1`  
   - mount หรือ extract เข้า container ที่รันอยู่  
2. **Nix profile / guix** ต่อ pack — reproducible, cache ดี  
3. **tarball + sha256 ใน object storage** — ง่ายสุด ควบคุมเวอร์ชันเอง  

สำคัญ: จากมุม agent ยังเป็น filesystem เดียว (`/usr/local`, `/opt/ctf-packs/…`)

#### ทริกเกอร์การดึง pack

เรียงความฉลาดจากง่าย → ดี:

1. **Heuristic ตอนเริ่ม challenge (prefetch)**  
   - มี `.apk` / `libapp.so` → prefetch `mobile`  
   - มี `Dockerfile` + PHP/WP → prefetch `web`  
   - มี ELF + `chall` + libc → prefetch `pwn`  
   - มี `.sage` / `output.txt` crypto → prefetch `crypto`  
2. **ตอนรันคำสั่งแล้วไม่เจอ binary**  
   - `bash: jadx: not found` → router รู้ว่าอยู่ใน pack `mobile` → ensure pack → retry คำสั่งครั้งเดียว  
3. **agent เรียก helper ชัดเจน** (optional, ไม่บังคับ)  
   - เช่น `ctf-ensure-pack mobile` ใน `/tools.txt`  

Prefetch สำคัญมาก — ไม่งั้นโจทย์แรกจะช้าเพราะ cold download

#### Cache

- เก็บที่โฮสต์: เช่น `~/.cache/ctf-agent/packs/`
- key = `packId + version + arch (arm64/amd64)`
- LRU หรือขนาดเพดาน (เช่น 20GB) — เกินแล้วลบ pack ที่ใช้นานสุด
- หลัง setup ครั้งแรกของแต่ละ pack → โจทย์หมวดเดิมไม่ต้องโหลดใหม่

---

## 5. สิ่งที่ไม่เปลี่ยน vs สิ่งที่เพิ่ม

### 5.1 ไม่เปลี่ยน (คง logic)

- CLI: `artemis` / `artemis swarm` (alias `ctf-solve`), coordinator loop, `--challenge`, `--models`
  (flags accepted locally — no external scoreboard)
- Tool surface ของ agent: `bash`, `read_file`, `write_file`, `list_files`, `submit_flag`, …
- หนึ่ง logical sandbox ต่อบริบทการแก้โจทย์
- โมเดล Cursor / coordinator ที่ใช้อยู่

### 5.2 เพิ่มใหม่ (โปร่งใส)

| ส่วน | หน้าที่ |
|------|---------|
| Pack registry | รายการ pack + version + checksum + arch |
| Pack cache (host) | เก็บ pack ที่เคยดึง |
| Ensure-pack | ติดตั้ง/mount pack เข้า container (โปร่งใสเมื่อ command หาย) |
| Prefetch จากไฟล์โจทย์ | ลด cold start (นามสกุล/ชื่อไฟล์ — ไม่อ่านคำใน description) |
| Router (บางๆ) | L0 → L1 ensure |
| `/tools.txt` อัปเดต | บอก agent ว่ามีเครื่องมืออะไร (ไม่โชว์ pack id) |
| Config | `CTF_PACK_CACHE`, `CTF_HOST_PROXY`, … |

### 5.3 สิ่งที่ควรแก้คู่กัน (คุณภาพ ไม่ใช่สถาปัตย์ packs)

แม้ไม่ใช่แกน lazy-packs แต่ควรทำไม่งั้น “tools ครบ” ก็ยังหลอกตัวเองเรื่อง flag:

1. **Dry-run `submit_flag` อย่ายืนยัน CORRECT อัตโนมัติ**  
   - แยก `submitted_candidate` กับ `verified_flag`  
   - อย่าขึ้น `FLAG FOUND` จาก test flag / guess
2. **Memory limit** ตาม pack (crypto/mobile ต้องการมากกว่า 4GB)
3. **Arch awareness** ใน `/tools.txt` (aarch64 vs ต้องใช้ qemu สำหรับ x86_64)

---

## 6. ตัวอย่าง flow จริง

### 6.1 PWNKnight (Flutter APK)

1. `artemis swarm --challenge ./challenges/pwnknight`
2. Prefetch: เห็น `.apk` → ดึง pack `mobile` (+ `rev` ถ้าจำเป็น)
3. Agent ใน L0+mobile: `jadx` / blutter / strings ตามปกติ
4. ไม่ต้องมี Kali เต็มบนเครื่อง

### 6.2 filtered-reality (web + bot)

1. Prefetch `web`
2. ถ้า bot ต้อง Chrome x86_64 บน ARM โฮสต์ — ใช้ qemu ใน pack `web` / donor ที่ออกแบบไว้
3. Agent logic เดิม

### 6.3 Crypto LWE (sage)

1. Prefetch `crypto` (sage ใหญ่ — ครั้งแรกช้า)
2. Cache ไว้ → โจทย์ crypto รอบหน้าเร็ว
3. ไม่ได้บังคับลูกค้าทุกคนโหลด sage ตั้งแต่วันแรก

---

## 7. ประสบการณ์ผู้ใช้ที่คาดหวัง

### 7.1 Onboarding (ครั้งเดียว)

```text
[1] ตรวจ Docker / Colima
[2] pull / bake L0 + donor images ที่โปรไฟล์เลือก (ครั้งเดียวตอนติดตั้ง)
[3] bake full Jeopardy pack caches (`artemis setup`) — pre-TUI prompt if missing
[4] พร้อมรัน artemis / artemis swarm
```

ลูกค้า**ไม่** bake ตอนเปิด challenge — bake/pull อยู่ในขั้นติดตั้ง / pre-TUI prompt  
(ใช้ ``uv run artemis setup`` หรือ `docker build` ตาม README เป็น fallback)  
Warm runtimes (`ctf-sandbox-warm-*`) ยัง optional; pack caches ไม่ใช่ optional สำหรับ prompt ก่อน TUI

แผน UX ระยะถัดไปเดิม (interactive shell) **shipped แล้ว** เป็น Artemis TUI —
ดู `README.md` / `chassis/AGENTS.md`

### 7.2 รายวัน (ปัจจุบัน — swarm)

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
cd /path/to/ctf
set -a && source .env && set +a
uv run artemis swarm --challenge ./challenges/foo --models cursor/composer-2.5 -v
```

ไม่ต้องจำว่า image ไหน — default คือ L0 + router

### 7.3 ความรู้สึกเรื่องน้ำหนัก

| เหตุการณ์ | ความรู้สึกที่ต้องการ |
|-----------|---------------------|
| ติดตั้งวันแรก | เร็ว (แค่ L0) |
| โจทย์แรกหมวดใหม่ | ช้าลงชั่วคราวตอนดึง pack |
| โจทย์หมวดเดิมซ้ำ | เร็ว (cache) |

---

## 8. ทำไมไม่ใช้ทางอื่น

| ทางเลือก | เหตุผลที่ไม่เลือกเป็นหลัก |
|----------|---------------------------|
| Mega Kali ในเครื่องทุกคน | หนักเกินไป, onboarding พัง, Mac อ่วม |
| หลาย image ให้ agent เลือกตอนรัน | เปลี่ยน logic / UX ที่ไม่อยากแตะ |
| เบาอย่างเดียว ไม่มี packs | ทำ PWNKnight / web bot / sage ไม่จบ |
| คลาวด์ล้วน (ย้าย sandbox) | ชน local-first / ลูกค้าบางรายไม่ส่งโจทย์ขึ้นเซิร์ฟเวอร์ |
| Bridge ไป Kali ลูกค้า (L2 เดิม) | ซับซ้อน, ขอบเขตสิทธิ์, ไม่จำเป็นถ้า L1 ครบ |
| apt ตอนรันทุกครั้งโดยไม่มี cache ออกแบบดี | ช้า ไม่ reproducible |

Lazy packs (L0 + L1) ชนะเพราะครบเงื่อนไขพร้อมกันภายใต้ข้อจำกัดปัจจุบัน

---

## 9. แผน implement

เรียงเฟส — ทำทีละขั้นโดยไม่พังของเดิม:

### Phase 0 — คุณภาพพื้นฐาน
- [x] Local `submit_flag` — CORRECT จบรัน; reject decoy (ไม่มี external scoreboard)
- [x] เอกสาร `/tools.txt` แยก L0 / pack
- [ ] ตั้ง RAM default ที่สมเหตุสมผลต่อโจทย์

### Phase 1 — L0 บาง + pack แรก
- [x] Dockerfile.core (L0) → `ctf-sandbox-core`
- [x] Pack format (donor image + path manifest) + cache dir
- [x] Additive `ensure_pack` (copy เข้า container เดิม — ไม่สลับ image)
- [x] Prefetch จากนามสกุลไฟล์โจทย์
- [x] Pack: `mobile`, `pwn`, `crypto`

### Phase 2 — Pack ครบหมวด (เทียบชุดต้นฉบับ)
- [x] `crypto` (Sage donor + bind cache)
- [x] `crypto-tools` (RsaCtfTool / cado-nfs / flatter / gmpy2 / fpylll)
- [x] `steg` (steghide / stegseek / zsteg / media / OCR)
- [x] `forensics` (sleuthkit / binwalk / volatility3 / tshark / scapy)
- [x] `web` (nmap / sqlmap / flask / PyJWT)
- [x] `ml` (torch CPU / keras)
- [x] `containers` (podman / buildah — best-effort)
- [x] `pwn` ขยาย angr + radare2
- [x] `linux` (linpeas / pspy / ffuf / katana / smbclient / sshpass / impacket / ldap-utils / certipy-ad / bloodhound-python / NetExec)
- [ ] ทดสอบโจทย์จริงทีละหมวด + build donors บน CI/เครื่อง dev (local bake ผ่าน ``artemis setup`` ได้แล้ว; CI automation ยังไม่บังคับ)
- [x] ขนาด cache + eviction (`CTF_PACK_CACHE_MAX_GB`, LRU via `.accessed`)
- [x] RAM floor ต่อ pack + ลบ fat image / `Dockerfile.sage` ออกจาก tree

### Phase 3 — Customer CLI setup / bake
เป้าหมาย: ลูกค้าไม่ต้องจำ `docker build -f …` — คำสั่งติดตั้งครั้งเดียวจบ

- [x] CLI setup: ``uv run artemis setup`` (และ ``--pack`` / ``--skip-core``) — แยกจาก solve loop
- [x] ตรวจ Docker / Colima + แนะนำ `DOCKER_HOST` บน Mac (`probe_docker_env` ใน setup)
- [x] Bake ตามรายการ pack: ขั้นต่ำ L0 + default common set (`mobile`/`pwn`/`ghidra`/…); ขยายด้วย ``--pack``
- [x] Idempotent + digest stale check (``pack_source_digest`` = Dockerfile + PackSpec/recipe fingerprint in ``.ready``; mismatch → rematerialize)
- [x] Lock ข้าม process ตอน bake (reuse pack extract flock — สอง `artemis setup` รอคิว)
- [x] ไม่ bake ตอน solve รายวัน — solve ใช้ image/cache ที่พร้อมแล้ว (missing donor = warning + fail-soft)
- [x] เอกสารชี้ ``artemis setup`` (README + `.env.example`); รายการ `docker build` คงเป็น fallback / CI

Interactive Ask/Agent shell is retired — product is the Artemis TUI single solve flow (`uv run artemis`). Hosted-brain SaaS remains deferred. CI bake automation still optional (local ``artemis setup`` is the customer path).

---

## 10. Config ร่าง (อนาคต — ยังไม่มี reader ในโค้ด)

ค่าด้านล่างเป็นร่างผลิตภัณฑ์เท่านั้น ยกเว้นที่ระบุว่า live แล้วใน `.env.example` /
`backend/tool_router.py` (`CTF_PACK_CACHE`, `CTF_PACK_STATE`, `CTF_PACK_BIND`, …)

```bash
# .env — ร่าง ไม่ได้บังคับใช้ตอนนี้

# L0
SANDBOX_IMAGE=ctf-sandbox-core

# L1 packs — NOT implemented yet (no os.environ readers):
CTF_PACK_REGISTRY=https://example.com/ctf-packs   # หรือ ghcr.io/...
CTF_PACK_AUTO=1                                   # prefetch + ensure on missing
CTF_PACK_PREFETCH=1
```

---

## 11. ความเสี่ยงและข้อควรรู้

1. **Cold start pack ใหญ่** (sage, chrome) — ต้องมี prefetch + progress ชัด ไม่งั้นดูเหมือนค้าง  
2. **Arch mismatch** — pack ต้องแยก `arm64` / `amd64`; blutter/Chrome อ่อนไหวมาก  
3. **False sense of “ครบ”** — มี pack แล้วยังต้องโมเดลดี + ไม่ accept flag ปลอม  
4. **blutter ต่อ Dart version** — pack `mobile` ต้อง version ตาม snapshot hash (เช่น `1ce86630…` ของ PWNKnight)
   blutter จะ fetch + compile Dart SDK ของ version นั้นเองในรันแรก (10–30 นาที) ผลลัพธ์เก็บบน host ที่
   `~/.cache/ctf-agent/pack-state/mobile/<arch>/var/cache/ctf-blutter` (**shared ข้าม session** —
   ไม่แยกตาม `ses_…`) bind RW เข้า container ทุกตัว จึงคอมไพล์ครั้งเดียวต่อ Dart version
   หลาย agent ที่รันพร้อมกัน share cache นี้ จึง serialize ด้วย `flock` ใน wrapper กัน build ชนกัน
   ลบ cache ก้อนนี้ = กลับไปคอมไพล์ใหม่ (ตั้ง `CTF_PACK_STATE` เพื่อย้ายที่เก็บ)
   Warm packs ล่วงหน้า: `artemis setup` (Phase 3) — เมื่อมี sample Flutter APK
   (`ARTEMIS_BLUTTER_WARM_APK` หรือใต้ `challenges/`) จะ prebuild Dart VM ด้วย

---

## 12. นโยบาย Skills / Prompt (บทเรียน PWNKnight 2026-07-20)

### 12.1 สิ่งที่ทดลองยืนยันแล้ว

| Setup | ผล |
|-------|-----|
| prompt เดิม + image ไม่มี blutter | วน strings / decoy — ไม่เข้าชั้น crypto |
| prompt เดิม + `ctf-sandbox-mobile` (มี blutter/jadx) | เข้า blutter → decrypt PIN/flag2 → RSA/EntropyBus ได้เอง |

**สรุป:** สำหรับโจทย์แนวนี้ **tools สำคัญกว่าการเพิ่ม skill/playbook**

### 12.2 นโยบายผลิตภัณฑ์ (ล็อก)

| ชั้น | ทำอะไร | ไม่ทำอะไร |
|------|--------|-----------|
| **Tools (L0–L1)** | ครบตามหมวด, discoverable, cache ได้ | — |
| **Prompt** | บาง: กติกา, path, ห้าม writeup, submit เฉพาะ flag จริง | ไม่ใส่ playbook หมวดละยาว |
| **Skills ภายนอก** (ctf-skills / ctf-kit) | optional อ้างอิงตอนคนพัฒนา pack / install list | **ห้าม inject ทั้ง repo เข้า solver ทุกรัน** |

เหตุผลห้าม inject ctf-skills เต็ม:

1. **Overfit** — เทคนิคจาก writeup/CTF เฉพาะชื่อ → ดึง agent ไปลอง pattern เก่า  
2. **Context bloat** — ร้อยไฟล์ markdown กินที่อ่าน asm/pp.txt  
3. **แย่กว่ารอบที่รันได้แล้ว** — PWNKnight สำเร็จด้วย tools ไม่ใช่ด้วยตำรา

ถ้าจะใช้ skill ในอนาคต: โหลด **หมวดเดียว บางมาก** ตอน triage ติดเท่านั้น ไม่ใช่ default

### 12.3 บทบาท 3 repo อ้างอิง (ไม่ใช่แกนรัน)

| Repo | บทบาทที่ถูกต้องในสถาปัตย์นี้ |
|------|-------------------------------|
| ljagiello/ctf-skills | แหล่งรายการ tools / เทคนิคอ้างอิงตอนออกแบบ pack — ไม่ใช่ system prompt |
| MysterionRise/ctf-kit | workflow ช่วยคนแข่ง (มนุษย์+AI) — ไม่แทน Veria swarm |
| foxibu/CTF-Solver | อ้างอิงแนวคิด Kali MCP เท่านั้น — **เราไม่ทำ L2 bridge**; ใช้ L1 packs แทน |

---

## 13. ทำไมมั่นใจว่าดีกว่าของที่รันอยู่ตอนนี้

สถานะปัจจุบัน (โค้ด): L0 `ctf-sandbox-core` + L1 additive `ensure_pack`
(`mobile` / `pwn` / `crypto` / `crypto-tools` / `steg` / `linux` / `forensics` /
`web` / `ml` / `containers`) + host pack cache  
Donors: `ctf-sandbox-mobile` / `pwn` / `crypto` / `crypto-tools` / `steg` / `linux`
(apt/pip-only packs reuse L0 as dummy donor)

| มิติ | ตอนนี้ | สถาปัตย์ L0–L1 (เป้าหมาย) | ทำไมดีกว่าแน่นอน |
|------|--------|---------------------------|------------------|
| UX | คำสั่งเดียว; prefetch/ensure เอง (`--image` เป็น override เท่านั้น) | คงเดิม + setup CLI | ลด human error / ไม่ลืมใส่ mobile |
| น้ำหนักเครื่อง | L0 เบา + pack ตามที่เคยใช้ | เหมือนกัน + cache eviction | onboarding + Mac/Colima อยู่ได้ |
| ความครบหมวด | pack ครบหมวดหลักแล้ว | pack registry ขยายได้ | ไม่ติด “ลืม build image ใหม่” |
| Skills อ้วน | ยังไม่ใส่ (ดี) | นโยบายห้าม inject เต็ม | ไม่ถอยหลังจากบทเรียน PWNKnight |
| คุณภาพ flag | Local accept + decoy reject | Race: candidate vs confirmed | จบรันเมื่อได้ flag จริง |

**สิ่งที่ทำให้ “ดีกว่านี้ไม่ได้” ภายใต้ข้อจำกัดเดียวกัน:**  
ถ้าเอา mega-Kali ทุกเครื่อง / หลาย sandbox ให้ agent เลือก / คลาวด์ล้วน / เบาอย่างเดียวไม่มี pack — จะชนข้อจำกัดข้อ 1 อย่างน้อยหนึ่งข้อ (ดู §8)

ทางเดียวที่ “อาจดีกว่า” คือเปลี่ยนข้อจำกัดผลิตภัณฑ์เอง ซึ่งไม่ใช่เป้าหมายปัจจุบัน

---

## 14. บทสรุปสำหรับทีม (ceiling)

สูตรสุดทาง:

```
Agent logic เดิม (บาง)
  + Sandbox Router โปร่งใส
  + L0 core เบา
  + L1 lazy packs (prefetch + cache + arch-aware)
  + Phase 0 quality (submit_flag / RAM / tools.txt)
  + ไม่ inject ctf-skills เต็ม
```

- **ไม่เปลี่ยน** วิธีที่ agent คิดและเรียก tools  
- **เปลี่ยน** ชั้นรันให้ฉลาด: เบาเป็นค่าเริ่ม ครบแบบ on-demand  
- **ทุกคน** → L0 + L1 (ไม่มี Kali bridge / ไม่ย้าย sandbox ขึ้นคลาวด์)  
- แผน hosted brain / SaaS → deferred (ไม่ใช่เฟส sandbox นี้)  
- Implement ตามเฟสในข้อ 9 — **อย่ากระโดดไปใส่ skill อ้วนแทน packs**

---

## 15. อ้างอิงจากบริบทโปรเจกต์นี้

| รายการ | ที่อยู่ / หมายเหตุ |
|--------|-------------------|
| Repo | Artemis TUI repo root (branch `cursor-backend`) |
| Sandbox ปัจจุบัน | L0 `Dockerfile.core`; packs `Dockerfile.pwn` / `crypto` / `crypto-tools` / `steg` / `linux` / `mobile`; apt/pip packs: forensics / web / ml / containers |
| Sandbox package | `backend/sandbox/` — `container` / `packs` / `proxy` / `harden` / `governor` / `docker_client`; public facade `from backend.sandbox import DockerSandbox` |
| Pack preflight | `backend/pack_preflight.py` — `force_packs` CLI `--pack` wins; else `detected_packs` / `detect_packs`; timings `preflight_ms`; `--eval-strict-packs` fail-closed |
| Resource governor | `backend/sandbox/governor.py` — memory floors via `recommended_memory_limit`; live `docker update`; CPU `NanoCpus` default 2e9 override `CTF_SANDBOX_NANO_CPUS` |
| Eval harness | `backend/eval_run.py` + CLI `--eval-out` / `--eval-max-wall-s` / `--eval-max-usd`; wall also caps in-flight sandbox `bash`; JSON `agent_failed` excludes infra-only deaths |
| Challenge load | Any operator path (Downloads/Desktop/`/Volumes`) except system/secret trees; launch inventory in `backend/launch_setup.py` + `guest_libs.py` |
| Fat / sage alias | **Removed** — use core + packs only |
| Cache | `~/.cache/ctf-agent/packs` + `CTF_PACK_CACHE_MAX_GB` (default 25) LRU eviction; `scripts/evict_pack_cache.py`, `scripts/prune_docker.sh` |
| Host VPN (Mac) | `backend/host_proxy.py` — auto SOCKS5 + proxychains in sandbox (`CTF_HOST_PROXY=auto`) so lab VPNs work without pf/routes |
| Image ที่พิสูจน์ mobile | `ctf-sandbox-mobile` (blutter Dart 3.10.4 prebuilt) |
| โจทย์ที่โชว์ช่องว่าง tools | `challenges/pwnknight`, `challenges/filtered-reality` |
| โจทย์ที่แกนเบาพอ | `challenges/do-you-have-good-eyes` |
| เอกสารนี้ | สถาปัตย์ล็อก L0+L1 — implement ตาม Phase 0→3; ผลิตภัณฑ์ TUI → `README.md` / `chassis/` |

เมื่อ implement แล้ว ให้อัปเดตสถานะจริงของแต่ละ Phase และลิงก์ไปยัง Dockerfile / pack manifest ที่สร้างขึ้น
