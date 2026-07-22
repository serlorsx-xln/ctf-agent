# WCTF23 Saw XI EP.2 — fair-eval handout

Source challenge: [WCTF23-Final-Round / Digital Forensics-Hard-Saw XI EP.2](https://github.com/wongyos/WCTF23-Final-Round)

Imported as `challenges/wctf23-saw-xi-ep2/` with **memory dump only** (no Drive URL in agent-visible text, no writeups).

- Handout: `distfiles/SAW.mem` (~2 GiB)
- Sealed answers file: `eval/sealed/wctf23-saw-xi-ep2-flags.json` (empty until you fill it)
- Upstream had no `flag.txt` in the repo

## Run

```bash
cd /Users/serlorsx/Downloads/ctf
unset DOCKER_HOST
uv run ctf-solve --challenge ./challenges/wctf23-saw-xi-ep2 --models 'cursor/grok-4.5' -v
```

Confirm candidates yourself. Do not paste writeups into prompts.
