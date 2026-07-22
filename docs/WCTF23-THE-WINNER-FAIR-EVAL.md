# WCTF23 The Winner — fair-eval handout

Source challenge: [WCTF23-Final-Round / Reverse Engineering & Pwn-Hard-The Winner](https://github.com/wongyos/WCTF23-Final-Round)

Imported as `challenges/wctf23-the-winner/` with **binary only** (no GitHub/Drive URLs, no writeups, no `pass.txt` / zip password theater — zip contents matched the ELF).

- Handout: `distfiles/the_winner` (ELF 64-bit, not stripped)
- Sealed answers: `eval/sealed/wctf23-the-winner-flags.json` (`expected_flag` null until you fill it)
- Upstream had no `flag.txt`; historical `nc` host is down — agents should use the local binary

## Run

```bash
cd /Users/serlorsx/Downloads/ctf
unset DOCKER_HOST
uv run ctf-solve --challenge ./challenges/wctf23-the-winner --models 'cursor/grok-4.5' -v
```

Confirm candidates yourself. Do not paste writeups into prompts.
