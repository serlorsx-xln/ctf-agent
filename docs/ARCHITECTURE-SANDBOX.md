# Sandbox Architecture — Lazy Packs + Optional Kali + Optional Cloud

เอกสารนี้อธิบายสถาปัตย์ที่เลือกไว้สำหรับ CTF Agent (Veria / Cursor backend fork)
เพื่อให้ **เบาบนเครื่องลูกค้า** แต่ **ทำโจทย์ได้ครบหมวด** โดยไม่เปลี่ยน logic หลักของระบบ

สถานะ: **สถาปัตย์ล็อกแล้ว** — Phase 0–1 ลงโค้ดแล้ว (L0 core + L1 additive packs + cache)  
อัปเดตล่าสุด: 2026-07-20  

| Phase | สถานะ |
|-------|--------|
| 0 local flag accept / tools.txt | ทำแล้ว |
| 1 L0 `Dockerfile.core` + additive `ensure_pack` + host cache + prefetch | ทำแล้ว |
| 2 packs (mobile / pwn / crypto / crypto-tools / steg / linux / forensics / web / ml / containers) | ทำแล้ว — ครอบคลุมชุดต้นฉบับ + linux box เบา |
| 3 L2 Kali | ยังไม่ |
| 4 L3 cloud | ยังไม่ |

โค้ดหลัก: `backend/tool_router.py`, `backend/sandbox.py` (`ensure_pack`), `sandbox/Dockerfile.core`  
Cache: `~/.cache/ctf-agent/packs/` (หรือ `CTF_PACK_CACHE`)

นี่คือทางที่ **ดีกว่านี้ไม่ได้ภายใต้ข้อจำกัดผลิตภัณฑ์ปัจจุบัน**  
(agent logic เดิม · local-first · เบาบนเครื่องลูกค้า · รองรับมี/ไม่มี Kali · ขยายเช่าได้)

---

## 1. เป้าหมายและข้อจำกัด

### 1.1 เป้าหมายผลิตภัณฑ์

1. Agent แก้โจทย์ CTF ได้หลายหมวด: crypto, pwn, web, rev, mobile, forensics, misc
2. เครื่องลูกค้าต้อง **เบา** — ไม่บังคับโหลด Kali/tools เต็ม (~10–20GB+) ทุกคน
3. รองรับลูกค้า **สองประเภท**:
   - **ไม่มี Kali** — ได้ความสามารถครบผ่านระบบของเรา
   - **มี Kali อยู่แล้ว** — ไม่ duplicate ของหนัก ใช้ของลูกค้าได้
4. รองรับอนาคต **ปล่อยเช่า / remote worker** โดยไม่ต้องรีดีไซน์ agent
5. **ไม่เปลี่ยน logic ระบบหลัก** ที่ใช้อยู่ตอนนี้

### 1.2 สิ่งที่ระบบปัจจุบันเป็นอยู่ (ต้องคงไว้)

```
ctf-solve / coordinator
  → สร้าง Docker sandbox ใบเดียวต่อ challenge (หรือ shared ตาม config)
  → agent เรียก tools: bash, read_file, write_file, list_files, submit_flag, …
  → คำสั่งรันใน sandbox
  → ได้ flag (local accept → FLAG_FOUND)
```

จุดสำคัญที่ **ห้ามทำลาย**:

- Agent ยังคิดว่ามี **sandbox เดียว** และพูดกับมันผ่าน bash/MCP เหมือนเดิม
- ไม่บังคับให้ agent เลือก image หลายใบตอน runtime (`--image crypto` / `--image pwn` สลับไปมา)
- Flow `ctf-solve --challenge …` / multi-challenge หลัง setup แล้วยังใช้คำสั่งเดิมได้

### 1.3 ปัญหาที่เจอจากการทดลองจริง

| ปัญหา | ตัวอย่าง |
|--------|----------|
| Image เบาเกินไป | `ctf-sandbox` (sage) ไม่มี blutter / r2 พอ → PWNKnight วน strings |
| Image จัดเต็ม | หนักเครื่อง, build นาน, RAM 4GB OOM, Mac ARM + Colima อึดอัด |
| Tools ผิดหมวด | `ctf-sandbox-pwn` ช่วย binary pwn แต่ไม่แก้ Flutter AOT โดยตรง |
| Flag decoys | `submit_flag` ต้อง reject placeholder / fake_flag |
| Host tools นอก sandbox | Kali บนโฮสต์ **เรียกใช้ไม่ได้** ด้วย architecture ปัจจุบัน (bash อยู่ใน container) |

บทเรียน: **ยัดครบใน image เดียวบนเครื่องลูกค้า = ไม่ไหว**  
แต่ **เบาอย่างเดียวโดยไม่มีทางขยาย = ทำโจทย์ยากไม่ได้**

---

## 2. สถาปัตย์ที่เลือก (สรุปหนึ่งบรรทัด)

> **หน้าตาเดิม (sandbox ใบเดียว + bash) — ข้างหลังฉลาด: แกนเบา + ดึง tool packs เฉพาะตอนต้องใช้ + ใช้ Kali ลูกค้าถ้ามี + คลาวด์เป็นตัวเลือก**

ภายใต้ข้อจำกัดของผลิตภัณฑ์นี้ นี่คือทางสถาปัตย์ที่ดีที่สุดแล้ว  
รายละเอียด implement (cache, prefetch) ปรับได้โดยไม่ต้องเปลี่ยนโครงใหญ่

---

## 3. ภาพรวมชั้น (L0–L3)

```
┌─────────────────────────────────────────────────────────────┐
│  Agent / Coordinator  (logic เดิม)                          │
│  bash / read_file / write_file / submit_flag                │
└────────────────────────────┬────────────────────────────────┘
                             │ API เดิม: “sandbox เดียว”
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  Sandbox Runtime Router  (ชั้นใหม่ — โปร่งใสต่อ agent)       │
│  - รับคำสั่ง bash                                            │
│  - ตรวจว่าต้องใช้ pack ไหน / Kali / cloud                     │
│  - ensure tools พร้อม แล้วค่อยรัน                            │
└───────┬─────────────────┬─────────────────┬─────────────────┘
        │                 │                 │
        ▼                 ▼                 ▼
   ┌────────┐       ┌──────────┐      ┌────────────┐
   │  L0    │       │  L1      │      │  L2 / L3   │
   │ Core   │◄─────►│ Lazy     │      │ Kali host  │
   │ เบา    │ mount │ Packs    │      │ / Cloud    │
   └────────┘       └──────────┘      └────────────┘
```

| ชั้น | ชื่อ | บทบาท | เมื่อไหร่ |
|------|------|--------|----------|
| **L0** | Core sandbox | รันตลอด เบา พอเริ่มโจทย์ | ทุกลูกค้า ทุกโจทย์ |
| **L1** | Lazy tool packs | ดึงชุด tools ตามความต้องการ | เจอไฟล์/หมวดที่ต้องใช้ |
| **L2** | Customer Kali bridge | ใช้ tools บน Kali ที่ลูกค้ามี | ลูกค้าประเภท “มี Kali” |
| **L3** | Cloud / rental worker | งานหนักมาก นอกเครื่องลูกค้า | โหมดเช่า หรือเครื่องอ่อน |

Agent **ไม่ต้องรู้** ว่าคำสั่งไป L1/L2/L3 — เห็นแค่ผล bash สำเร็จ/ล้มเหลว

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
- Kali metapackages

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
| `pwn` | binary exploitation | qemu-user-static, gef/pwndbg, one_gadget, patchelf, seccomp-tools, ROPgadget, r2 (ถ้าไม่ได้อยู่ใน core) |
| `crypto` | cryptography | sagemath หรือ sage-lite + fpylll / ที่จำเป็นต่อ LWE-CVP |
| `web` | web | chromium/playwright deps, phpggc, sqlmap (optional), nikto เบาๆ |
| `rev` | reverse engineering | ghidra headless หรือ rizin เต็ม, radare2 plugins |
| `mobile` | Android/iOS CTF | jadx, apktool, uber-apk-signer, **blutter** (หรือ prebuilt ตาม Dart snapshot hash), frida-tools |
| `forensics` | forensics / stego | binwalk, foremost, steghide, exiftool, tesseract, sleuthkit (เลือกย่อยได้) |
| `osint` | misc/osint | เครื่องมือเบาเฉพาะทาง (optional) |

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

### 4.3 L2 — Customer Kali Bridge (ลูกค้ามี Kali)

#### ปัญหา

ตอนนี้คำสั่งอยู่ใน Docker → binary บน Kali โฮสต์ **ไม่อยู่ใน PATH** → เรียกไม่ได้

#### เป้าหมาย L2

ลูกค้าประเภท A ตอน setup บอกว่า:

```bash
# ตัวอย่าง config (ยังไม่ implement)
CTF_EXECUTOR=hybrid
KALI_EXEC_MODE=ssh   # หรือ docker | local-path
KALI_SSH=user@127.0.0.1
# หรือ
KALI_BIN_PREFIX=/usr  # ถ้า mount Kali root แบบระมัดระวัง
```

#### พฤติกรรม

- คำสั่งที่ L0/L1 มีแล้ว → รันใน sandbox ตามปกติ
- คำสั่งที่ map ไป Kali (หรือ pack หนักที่ลูกค้ามีบน Kali แล้ว) → router ส่งไป Kali
- ผลลัพธ์ (stdout/stderr/exit) กลับมาแบบเดียวกับ bash ใน sandbox
- ไฟล์ challenge ต้องมองเห็นฝั่ง Kali ได้ (mount ร่วม / sync workspace / scp ชั่วคราว)

#### ข้อควรระวังความปลอดภัย

- อย่า mount Docker socket ให้ agent แบบเปิดกว้างโดยไม่จำเป็น
- SSH ใช้ key แยก, command allowlist ถ้าเป็นไปได้
- แยกชัดว่า “โจทย์อยู่ใน workspace ที่ sync” ไม่ให้ agent เดินทั้งเครื่องลูกค้า

#### ประสบการณ์ลูกค้าประเภท A

- ไม่ต้องโหลด `crypto`/`rev` ซ้ำถ้า Kali มีอยู่แล้ว
- เครื่องไม่กิน Docker ซ้อนของหนัก
- ยังใช้คำสั่ง `ctf-solve` ชุดเดิม

---

### 4.4 L3 — Cloud / Rental Worker (อนาคต)

ใช้เมื่อ:

- ลูกค้าปล่อยเช่า / BYOK แต่เครื่องอ่อน
- งานหนักมาก: build blutter, sage memory สูง, headless Chrome bot บน arch ถูกต้องการ
- ต้องการ pool หลายเครื่อง

#### พฤติกรรม

- API เดิมเหมือน L0 (`bash` ใน “sandbox”)
- Router ส่ง job ไป worker ephemeral
- Challenge files: encrypt in transit, TTL สั้น, **zero retention** หลังจบ (สอดคล้องข้อกังวล IP โจทย์ลูกค้า)
- Billing / hand system อยู่ชั้นนี้ (อนาคต) — ไม่ปนกับ L0 local

#### สิ่งที่ยังไม่ทำตอนนี้

- ระบบ hand / คิวงาน / multi-tenant isolation ละเอียด  
เอกสารนี้แค่จองที่วาง L3 ไว้ให้โครงไม่พังตอนขยาย

---

## 5. ลูกค้าสองประเภท — ตอนติดตั้ง ไม่ใช่ตอนรัน

| | ประเภท B: ไม่มี Kali | ประเภท A: มี Kali |
|--|----------------------|-------------------|
| Setup | ติดตั้ง L0 + ตั้ง pack registry/cache | ติดตั้ง L0 + เปิด L2 ชี้ Kali |
| ครั้งแรกที่เจอโจทย์หนัก | ดาวน์โหลด L1 pack ที่เกี่ยวข้อง | ใช้ Kali (L2) หรือ pack เฉพาะที่ Kali ไม่มี (เช่น blutter) |
| คำสั่งรายวัน | `ctf-solve …` เหมือนกัน | เหมือนกัน |
| น้ำหนักเครื่อง | เบา + โตตาม pack ที่เคยใช้ | เบากว่า — ของหนักอยู่ที่ Kali อยู่แล้ว |

**ห้าม** ให้ agent ตอน runtime ถามว่า “คุณมี Kali ไหมแล้วสลับ image”  
ตัดสินใจจบที่ **config ตอน setup**

---

## 6. สิ่งที่ไม่เปลี่ยน vs สิ่งที่เพิ่ม

### 6.1 ไม่เปลี่ยน (คง logic)

- CLI: `ctf-solve`, coordinator loop, `--challenge`, `--models`
  (flags accepted locally — no external scoreboard)
- Tool surface ของ agent: `bash`, `read_file`, `write_file`, `list_files`, `submit_flag`, …
- หนึ่ง logical sandbox ต่อบริบทการแก้โจทย์
- โมเดล Cursor / coordinator ที่ใช้อยู่

### 6.2 เพิ่มใหม่ (โปร่งใส)

| ส่วน | หน้าที่ |
|------|---------|
| Pack registry | รายการ pack + version + checksum + arch |
| Pack cache (host) | เก็บ pack ที่เคยดึง |
| Ensure-pack | ติดตั้ง/mount pack เข้า container (โปร่งใสเมื่อ command หาย) |
| Prefetch จากไฟล์โจทย์ | ลด cold start (นามสกุล/ชื่อไฟล์ — ไม่อ่านคำใน description) |
| Router (บางๆ) | L0 → L1 → L2 → L3 |
| `/tools.txt` อัปเดต | บอก agent ว่ามีเครื่องมืออะไร (ไม่โชว์ pack id) |
| Config | `CTF_PACK_CACHE`, `CTF_HOST_PROXY`, `CTF_EXECUTOR`, `KALI_*`, `CTF_CLOUD_*` |

### 6.3 สิ่งที่ควรแก้คู่กัน (คุณภาพ ไม่ใช่สถาปัตย์ packs)

แม้ไม่ใช่แกน lazy-packs แต่ควรทำไม่งั้น “tools ครบ” ก็ยังหลอกตัวเองเรื่อง flag:

1. **Dry-run `submit_flag` อย่ายืนยัน CORRECT อัตโนมัติ**  
   - แยก `submitted_candidate` กับ `verified_flag`  
   - อย่าขึ้น `FLAG FOUND` จาก test flag / guess
2. **Memory limit** ตาม pack (crypto/mobile ต้องการมากกว่า 4GB)
3. **Arch awareness** ใน `/tools.txt` (aarch64 vs ต้องใช้ qemu สำหรับ x86_64)

---

## 7. ตัวอย่าง flow จริง

### 7.1 PWNKnight (Flutter APK) — ลูกค้าไม่มี Kali

1. `ctf-solve --challenge ./challenges/pwnknight`
2. Prefetch: เห็น `.apk` → ดึง pack `mobile` (+ `rev` ถ้าจำเป็น)
3. Agent ใน L0+mobile: `jadx` / blutter / strings ตามปกติ
4. ไม่ต้องมี Kali เต็มบนเครื่อง

### 7.2 PWNKnight — ลูกค้ามี Kali + jadx อยู่แล้ว

1. Setup: L2 เปิดอยู่
2. Prefetch เบา หรือข้าม pack ที่ Kali มี
3. Router ส่ง `jadx` ไป Kali; sandbox ยังถือ workspace
4. ถ้า Kali ไม่มี blutter → ดึงแค่ pack `mobile-flutter` จาก L1

### 7.3 filtered-reality (web + bot)

1. Prefetch `web`
2. ถ้า bot ต้อง Chrome x86_64 บน ARM โฮสต์ — router อาจส่ง L3 หรือใช้ qemu ใน pack `web`
3. Agent logic เดิม; ไม่รู้ว่า Chrome มาจากชั้นไหน

### 7.4 Crypto LWE (sage)

1. Prefetch `crypto` (sage ใหญ่ — ครั้งแรกช้า)
2. Cache ไว้ → โจทย์ crypto รอบหน้าเร็ว
3. ไม่ได้บังคับลูกค้าทุกคนโหลด sage ตั้งแต่วันแรก

---

## 8. ประสบการณ์ผู้ใช้ที่คาดหวัง

### 8.1 Onboarding (ครั้งเดียว)

```text
[1] ตรวจ Docker / Colima
[2] เลือกโปรไฟล์:
      (B) Standard — ไม่มี Kali  → L0 + pack registry
      (A) I have Kali            → L0 + L2 bridge
      (C) Cloud worker           → L0 local + L3 (อนาคต)
[3] pull L0 image (เล็ก, เร็ว)
[4] (optional) prefetch packs ยอดนิยมที่เลือก: pwn, web, …
[5] พร้อมรัน ctf-solve
```

### 8.2 รายวัน

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
cd /path/to/ctf
set -a && source .env && set +a
uv run ctf-solve --challenge ./challenges/foo --models cursor/composer-2.5 -v
```

ไม่ต้องจำว่า image ไหน — default คือ L0 + router

### 8.3 ความรู้สึกเรื่องน้ำหนัก

| เหตุการณ์ | ความรู้สึกที่ต้องการ |
|-----------|---------------------|
| ติดตั้งวันแรก | เร็ว (แค่ L0) |
| โจทย์แรกหมวดใหม่ | ช้าลงชั่วคราวตอนดึง pack |
| โจทย์หมวดเดิมซ้ำ | เร็ว (cache) |
| มี Kali | เบาตลอดเกือบทั้งหมด |

---

## 9. ทำไมไม่ใช้ทางอื่น

| ทางเลือก | เหตุผลที่ไม่เลือกเป็นหลัก |
|----------|---------------------------|
| Mega Kali ในเครื่องทุกคน | หนักเกินไป, onboarding พัง, Mac อ่วม |
| หลาย image ให้ agent เลือกตอนรัน | เปลี่ยน logic / UX ที่ไม่อยากแตะ |
| เบาอย่างเดียว ไม่มี packs | ทำ PWNKnight / web bot / sage ไม่จบ |
| คลาวด์ล้วน | ชน local-first / ลูกค้าบางรายไม่ส่งโจทย์ขึ้นเซิร์ฟเวอร์ |
| apt ตอนรันทุกครั้งโดยไม่มี cache ออกแบบดี | ช้า ไม่ reproducible |

Lazy packs + optional Kali + optional cloud ชนะเพราะครบเงื่อนไขพร้อมกัน

---

## 10. แผน implement

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
- [x] `forensics` (sleuthkit / binwalk / volatility3 / carving)
- [x] `web` (nmap / flask / PyJWT)
- [x] `ml` (torch CPU / keras)
- [x] `containers` (podman / buildah — best-effort)
- [x] `pwn` ขยาย angr + radare2
- [x] `linux` (linpeas / pspy / ffuf / smbclient / sshpass / impacket)
- [ ] ทดสอบโจทย์จริงทีละหมวด + build donors บน CI/เครื่อง dev
- [x] ขนาด cache + eviction (`CTF_PACK_CACHE_MAX_GB`, LRU via `.accessed`)
- [x] RAM floor ต่อ pack + ลบ fat image / `Dockerfile.sage` ออกจาก tree

### Phase 3 — L2 Kali bridge
- [ ] Config + SSH/exec adapter
- [ ] Sync workspace
- [ ] เอกสาร setup สำหรับลูกค้าประเภท A

### Phase 4 — L3 Cloud worker
- [ ] Worker protocol เข้ากับ bash API เดิม
- [ ] Ephemeral storage + zero retention
- [ ] Hand / billing hooks (แยกบริการ)

---

## 11. Config ร่าง (อนาคต)

```bash
# .env — ร่าง ไม่ได้บังคับใช้ตอนนี้

# L0
SANDBOX_IMAGE=ctf-sandbox-core

# L1 packs
CTF_PACK_REGISTRY=https://example.com/ctf-packs   # หรือ ghcr.io/...
CTF_PACK_CACHE=$HOME/.cache/ctf-agent/packs
CTF_PACK_AUTO=1                                   # prefetch + ensure on missing
CTF_PACK_PREFETCH=1

# L2 Kali (ประเภท A)
CTF_EXECUTOR=local          # local | hybrid | cloud
KALI_ENABLED=0
KALI_SSH=
KALI_WORKDIR=

# L3 cloud (อนาคต)
CTF_CLOUD_URL=
CTF_CLOUD_TOKEN=
CTF_CLOUD_ZERO_RETENTION=1
```

---

## 12. ความเสี่ยงและข้อควรรู้

1. **Cold start pack ใหญ่** (sage, chrome) — ต้องมี prefetch + progress ชัด ไม่งั้นดูเหมือนค้าง  
2. **Arch mismatch** — pack ต้องแยก `arm64` / `amd64`; blutter/Chrome อ่อนไหวมาก  
3. **สิทธิ์และความปลอดภัย L2** — bridge ไป Kali ต้องจำกัดขอบเขต  
4. **IP โจทย์ลูกค้าบน L3** — ต้อง ephemeral + นโยบายชัด  
5. **False sense of “ครบ”** — มี pack แล้วยังต้องโมเดลดี + ไม่ accept flag ปลอม  
6. **blutter ต่อ Dart version** — pack `mobile` ต้อง version ตาม snapshot hash (เช่น `1ce86630…` ของ PWNKnight)

---

## 13. นโยบาย Skills / Prompt (บทเรียน PWNKnight 2026-07-20)

### 13.1 สิ่งที่ทดลองยืนยันแล้ว

| Setup | ผล |
|-------|-----|
| prompt เดิม + image ไม่มี blutter | วน strings / decoy — ไม่เข้าชั้น crypto |
| prompt เดิม + `ctf-sandbox-mobile` (มี blutter/jadx) | เข้า blutter → decrypt PIN/flag2 → RSA/EntropyBus ได้เอง |

**สรุป:** สำหรับโจทย์แนวนี้ **tools สำคัญกว่าการเพิ่ม skill/playbook**

### 13.2 นโยบายผลิตภัณฑ์ (ล็อก)

| ชั้น | ทำอะไร | ไม่ทำอะไร |
|------|--------|-----------|
| **Tools (L0–L3)** | ครบตามหมวด, discoverable, cache ได้ | — |
| **Prompt** | บาง: กติกา, path, ห้าม writeup, submit เฉพาะ flag จริง | ไม่ใส่ playbook หมวดละยาว |
| **Skills ภายนอก** (ctf-skills / ctf-kit) | optional อ้างอิงตอนคนพัฒนา pack / install list | **ห้าม inject ทั้ง repo เข้า solver ทุกรัน** |

เหตุผลห้าม inject ctf-skills เต็ม:

1. **Overfit** — เทคนิคจาก writeup/CTF เฉพาะชื่อ → ดึง agent ไปลอง pattern เก่า  
2. **Context bloat** — ร้อยไฟล์ markdown กินที่อ่าน asm/pp.txt  
3. **แย่กว่ารอบที่รันได้แล้ว** — PWNKnight สำเร็จด้วย tools ไม่ใช่ด้วยตำรา

ถ้าจะใช้ skill ในอนาคต: โหลด **หมวดเดียว บางมาก** ตอน triage ติดเท่านั้น ไม่ใช่ default

### 13.3 บทบาท 3 repo อ้างอิง (ไม่ใช่แกนรัน)

| Repo | บทบาทที่ถูกต้องในสถาปัตย์นี้ |
|------|-------------------------------|
| ljagiello/ctf-skills | แหล่งรายการ tools / เทคนิคอ้างอิงตอนออกแบบ pack — ไม่ใช่ system prompt |
| MysterionRise/ctf-kit | workflow ช่วยคนแข่ง (มนุษย์+AI) — ไม่แทน Veria swarm |
| foxibu/CTF-Solver | แนวคิด L2 (Kali MCP) — รวมผ่าน bridge ไม่ซ้อน MCP คู่ swarm |

---

## 14. ทำไมมั่นใจว่าดีกว่าของที่รันอยู่ตอนนี้

สถานะปัจจุบัน (โค้ด): L0 `ctf-sandbox-core` + L1 additive `ensure_pack`
(`mobile` / `pwn` / `crypto` / `crypto-tools` / `steg` / `linux` / `forensics` /
`web` / `ml` / `containers`) + host pack cache  
Donors: `ctf-sandbox-mobile` / `pwn` / `crypto` / `crypto-tools` / `steg` / `linux`
(apt/pip-only packs reuse L0 as dummy donor)

| มิติ | ตอนนี้ | สถาปัตย์ L0–L3 (เป้าหมาย) | ทำไมดีกว่าแน่นอน |
|------|--------|---------------------------|------------------|
| UX | คำสั่งเดียว; prefetch/ensure เอง (`--image` เป็น override เท่านั้น) | คงเดิม + L2/L3 | ลด human error / ไม่ลืมใส่ mobile |
| น้ำหนักเครื่อง | L0 เบา + pack ตามที่เคยใช้ | เหมือนกัน + cache eviction | onboarding + Mac/Colima อยู่ได้ |
| ความครบหมวด | pack ครบหมวดหลักแล้ว | pack registry ขยายได้ | ไม่ติด “ลืม build image ใหม่” |
| ลูกค้ามี Kali | ใช้ไม่ได้จากใน container | L2 bridge | ไม่ duplicate ของหนัก |
| อนาคตเช่า | ต้องรีดีไซน์ | L3 เสียบเข้า router | ไม่พัง agent API |
| Skills อ้วน | ยังไม่ใส่ (ดี) | นโยบายห้าม inject เต็ม | ไม่ถอยหลังจากบทเรียน PWNKnight |
| คุณภาพ flag | Local accept + decoy reject | Race: candidate vs confirmed | จบรันเมื่อได้ flag จริง |

**สิ่งที่ทำให้ “ดีกว่านี้ไม่ได้” ภายใต้ข้อจำกัดเดียวกัน:**  
ถ้าเอา mega-Kali ทุกเครื่อง / หลาย sandbox ให้ agent เลือก / คลาวด์ล้วน / เบาอย่างเดียวไม่มี pack — จะชนข้อจำกัดข้อ 1 อย่างน้อยหนึ่งข้อ (ดู §9)

ทางเดียวที่ “อาจดีกว่า” คือเปลี่ยนข้อจำกัดผลิตภัณฑ์เอง (เช่น บังคับ cloud-only หรือบังคับ Kali ทุกคน) ซึ่งไม่ใช่เป้าหมายปัจจุบัน

---

## 15. บทสรุปสำหรับทีม (ceiling)

สูตรสุดทาง:

```
Agent logic เดิม (บาง)
  + Sandbox Router โปร่งใส
  + L0 core เบา
  + L1 lazy packs (prefetch + cache + arch-aware)
  + L2 optional Kali
  + L3 optional cloud
  + Phase 0 quality (submit_flag / RAM / tools.txt)
  + ไม่ inject ctf-skills เต็ม
```

- **ไม่เปลี่ยน** วิธีที่ agent คิดและเรียก tools  
- **เปลี่ยน** ชั้นรันให้ฉลาด: เบาเป็นค่าเริ่ม ครบแบบ on-demand  
- **ลูกค้าไม่มี Kali** → L0 + L1  
- **ลูกค้ามี Kali** → L0 + L2 (+ L1 เฉพาะที่ขาด)  
- **เช่าในอนาคต** → เติม L3 โดยไม่รีดีไซน์ agent  
- Implement ตามเฟสในข้อ 10 — **อย่ากระโดดไปใส่ skill อ้วนแทน packs**

---

## 16. อ้างอิงจากบริบทโปรเจกต์นี้

| รายการ | ที่อยู่ / หมายเหตุ |
|--------|-------------------|
| Repo | `/Users/serlorsx/Downloads/ctf` (branch Cursor backend) |
| Sandbox ปัจจุบัน | L0 `Dockerfile.core`; packs `Dockerfile.pwn` / `crypto` / `crypto-tools` / `steg` / `linux` / `mobile`; apt/pip packs: forensics / web / ml / containers |
| Fat / sage alias | **Removed** — use core + packs only |
| Cache | `~/.cache/ctf-agent/packs` + `CTF_PACK_CACHE_MAX_GB` (default 25) LRU eviction; `scripts/evict_pack_cache.py`, `scripts/prune_docker.sh` |
| Host VPN (Mac) | `backend/host_proxy.py` — auto SOCKS5 + proxychains in sandbox (`CTF_HOST_PROXY=auto`) so lab VPNs work without pf/routes |
| Image ที่พิสูจน์ mobile | `ctf-sandbox-mobile` (blutter Dart 3.10.4 prebuilt) |
| โจทย์ที่โชว์ช่องว่าง tools | `challenges/pwnknight`, `challenges/filtered-reality` |
| โจทย์ที่แกนเบาพอ | `challenges/do-you-have-good-eyes` |
| เอกสารนี้ | สถาปัตย์ล็อก — implement ตาม Phase 0→4 |

เมื่อ implement แล้ว ให้อัปเดตสถานะจริงของแต่ละ Phase และลิงก์ไปยัง Dockerfile / pack manifest ที่สร้างขึ้น
