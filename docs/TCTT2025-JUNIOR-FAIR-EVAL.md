# TCTT2025 Junior Qualifier — fair-eval handouts

Source: [nyaouu/TCTT2025-Junior-Qualifier](https://github.com/nyaouu/TCTT2025-Junior-Qualifier)  
Imported as **clean handouts only** (no writeups / screenshots / `flag.txt` / `solve.py`).

## Challenges

Folders: `challenges/tctt-junior-*`

Sandbox mounts only `challenge.txt` + `distfiles/`.

Human-only expected flags: `eval/sealed/tctt2025-junior-flags.json` (gitignored, mode 0600).  
Do not paste sealed flags into prompts.

Missing sealed flags upstream (no `writeup/flag.txt`): `tctt-junior-secret-or-see-real`, `tctt-junior-re-pwn-300`.

## Run

```bash
cd /Users/serlorsx/Downloads/ctf
unset DOCKER_HOST
uv run ctf-solve --challenge ./challenges/tctt-junior-very-ez-re --models 'cursor/grok-4.5' -v
```

Confirm with sealed JSON yourself (y/N). Never put writeup repo contents back under `challenges/`.
