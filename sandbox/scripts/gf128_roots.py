#!/usr/bin/env python3
"""GF(2^128) polynomial roots — NIST AES-GCM field, low memory.

Usage:
  gf128-roots --demo
  gf128-roots --coeffs HEX0,HEX1,...,HEXd   # low degree first (16-byte hex each)

Uses the standard NIST GF(2^128) multiply (same field as AES-GCM).
Keeps polynomial degree small (Cantor–Zassenhaus / abs-trace).
"""

from __future__ import annotations

import argparse
import random
import sys

_MASK = (1 << 128) - 1
_R = 0xE1000000000000000000000000000000
ZERO = 0
ONE = 1 << 127  # NIST GCM bit order: constant-1 is 0x80||00...


def b2i(b: bytes) -> int:
    if len(b) != 16:
        raise ValueError("need 16 bytes")
    return int.from_bytes(b, "big")


def i2b(x: int) -> bytes:
    return (x & _MASK).to_bytes(16, "big")


def gf_mul(a: int, b: int) -> int:
    X = a & _MASK
    V = b & _MASK
    Z = 0
    for i in range(128):
        if (X >> (127 - i)) & 1:
            Z ^= V
        lsb = V & 1
        V >>= 1
        if lsb:
            V ^= _R
    return Z


def gf_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError
    r, base, e = ONE, a, (1 << 128) - 2
    # ONE is field 1; but pow should start at 1 = ONE in this representation
    r = ONE
    while e:
        if e & 1:
            r = gf_mul(r, base)
        base = gf_mul(base, base)
        e >>= 1
    return r


class Poly:
    __slots__ = ("c",)

    def __init__(self, coeffs: list[int]):
        self.c = [x & _MASK for x in coeffs]
        self._norm()

    def _norm(self) -> None:
        while len(self.c) > 1 and self.c[-1] == 0:
            self.c.pop()
        if not self.c:
            self.c = [0]

    def deg(self) -> int:
        self._norm()
        return -1 if self.c == [0] else len(self.c) - 1

    def monic(self) -> Poly:
        if self.deg() < 0:
            return Poly([0])
        inv = gf_inv(self.c[-1])
        return Poly([gf_mul(x, inv) for x in self.c])

    def add(self, o: Poly) -> Poly:
        n = max(len(self.c), len(o.c))
        return Poly(
            [
                (self.c[i] if i < len(self.c) else 0)
                ^ (o.c[i] if i < len(o.c) else 0)
                for i in range(n)
            ]
        )

    def mul(self, o: Poly) -> Poly:
        if self.deg() < 0 or o.deg() < 0:
            return Poly([0])
        out = [0] * (len(self.c) + len(o.c) - 1)
        for i, a in enumerate(self.c):
            if not a:
                continue
            for j, b in enumerate(o.c):
                if b:
                    out[i + j] ^= gf_mul(a, b)
        return Poly(out)

    def mod(self, m: Poly) -> Poly:
        r = Poly(list(self.c))
        dm = m.deg()
        lead_inv = gf_inv(m.c[-1])
        while r.deg() >= dm:
            f = gf_mul(r.c[-1], lead_inv)
            s = r.deg() - dm
            for i, mc in enumerate(m.c):
                if mc:
                    idx = i + s
                    while len(r.c) <= idx:
                        r.c.append(0)
                    r.c[idx] ^= gf_mul(f, mc)
            r._norm()
        return r

    def gcd(self, o: Poly) -> Poly:
        a, b = self.monic(), o.monic()
        while b.deg() >= 0:
            a, b = b, a.mod(b).monic()
        return a.monic()

    def eval(self, x: int) -> int:
        y = 0
        for c in reversed(self.c):
            y = gf_mul(y, x) ^ c
        return y


def poly_divmod(a: Poly, b: Poly) -> tuple[Poly, Poly]:
    b = b.monic()
    dm = b.deg()
    r = Poly(list(a.c))
    lead_inv = gf_inv(b.c[-1])
    qmap: dict[int, int] = {}
    while r.deg() >= dm:
        f = gf_mul(r.c[-1], lead_inv)
        s = r.deg() - dm
        qmap[s] = qmap.get(s, 0) ^ f
        for i, mc in enumerate(b.c):
            if mc:
                idx = i + s
                while len(r.c) <= idx:
                    r.c.append(0)
                r.c[idx] ^= gf_mul(f, mc)
        r._norm()
    qc = [0]
    if qmap:
        qc = [0] * (max(qmap) + 1)
        for i, v in qmap.items():
            qc[i] = v
    return Poly(qc), r


def random_poly(deg: int, rng: random.Random) -> Poly:
    if deg <= 0:
        return Poly([ONE])
    return Poly([rng.randrange(1 << 128) for _ in range(deg)] + [ONE])


def abs_trace_poly(h: Poly, mod: Poly) -> Poly:
    term = h.mod(mod)
    acc = term
    for _ in range(127):
        term = term.mul(term).mod(mod)
        acc = acc.add(term)
    return acc


def find_roots(f: Poly, seed: int = 0xC7F) -> list[int]:
    f = f.monic()
    if f.deg() < 0:
        return []

    der = [0] * max(len(f.c) - 1, 1)
    for i in range(1, len(f.c)):
        if i & 1:
            der[i - 1] ^= f.c[i]
    gsq = f.gcd(Poly(der))
    if gsq.deg() > 0:
        q, rem = poly_divmod(f, gsq)
        if rem.c == [0]:
            f = q.monic()

    rng = random.Random(seed)
    factors = [f]
    linears: list[int] = []
    guard = 0
    while factors and guard < 250:
        guard += 1
        p = factors.pop().monic()
        d = p.deg()
        if d <= 0:
            continue
        if d == 1:
            linears.append(gf_mul(p.c[0], gf_inv(p.c[1])))
            continue

        split = False
        for _ in range(48):
            h = random_poly(max(d - 1, 1), rng)
            t = abs_trace_poly(h, p)
            g1 = p.gcd(t)
            if 0 < g1.deg() < d:
                q, rem = poly_divmod(p, g1)
                if rem.c == [0]:
                    factors.append(g1.monic())
                    factors.append(q.monic())
                    split = True
                    break
        if split:
            continue

    out: list[int] = []
    for r in linears:
        if f.eval(r) == 0 and r not in out:
            out.append(r)
    return out


def parse_coeffs(s: str) -> list[int]:
    out = []
    for p in s.split(","):
        p = p.strip().lower().removeprefix("0x")
        if not p:
            continue
        out.append(b2i(bytes.fromhex(p)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coeffs")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        H = b2i(bytes(range(16)))
        p = Poly([gf_mul(H, ONE), H ^ ONE, ONE])  # (x+H)(x+1)
        assert p.eval(H) == 0 and p.eval(ONE) == 0
        roots = find_roots(p)
        ok = set(roots) == {H, ONE}
        print("demo_ok" if ok else "demo_fail", [i2b(r).hex() for r in roots])
        return 0 if ok else 1

    if not args.coeffs:
        ap.print_help()
        return 2
    for r in find_roots(Poly(parse_coeffs(args.coeffs))):
        print(i2b(r).hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
