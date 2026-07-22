"""Challenge network-hint parsing and command hardening."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("ctf.sandbox")


def parse_challenge_network_hints(text: str) -> tuple[list[str], list[int]]:
    """Extract lab hosts (IPs + lab-ish FQDNs) and TCP ports from challenge text.

    Generic patterns only — not challenge-specific service lists.
    Hostnames are included so Mac+VPN calibration can probe when no RFC1918 IP
    is pasted (common Assumed Breach writeups with only ``dc.lab.htb``).
    """
    import re

    hosts: list[str] = []
    seen_h: set[str] = set()

    def _add_host(h: str) -> None:
        h = h.strip().rstrip(".").lower()
        if not h or h in seen_h or h.startswith("127."):
            return
        if h in {"localhost", "example.com", "example.org"}:
            return
        # Docker Desktop / Colima gateway — not a remote VPN lab.
        if h == "host.docker.internal" or h.endswith(".docker.internal"):
            return
        seen_h.add(h)
        hosts.append(h)

    for m in re.finditer(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b",
        text,
    ):
        _add_host(m.group(0))

    # Lab-ish DNS names (htb/thm/local/…) — not a general TLD grab.
    for m in re.finditer(
        r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
        r"(?:htb|thm|local|lab|internal|lan|corp|vuln|offline)\b",
        text,
        flags=re.I,
    ):
        _add_host(m.group(0))

    # nc host port / connect host port
    for m in re.finditer(
        r"\bnc\s+([A-Za-z0-9._-]+)\s+(\d{2,5})\b",
        text,
        flags=re.I,
    ):
        _add_host(m.group(1))

    ports: list[int] = []
    seen_p: set[int] = set()

    def _add_port(raw: str) -> None:
        try:
            p = int(raw)
        except ValueError:
            return
        if 1 <= p <= 65535 and p not in seen_p:
            seen_p.add(p)
            ports.append(p)

    # host:port / :port after an IP or hostname
    for m in re.finditer(
        r"(?:(?:10|172|192)\.[\d.]+|localhost|127\.0\.0\.1|[A-Za-z0-9._-]+\.(?:htb|thm|local|lab))"
        r":(\d{1,5})\b",
        text,
        flags=re.I,
    ):
        _add_port(m.group(1))

    for m in re.finditer(
        r"\bnc\s+[A-Za-z0-9._-]+\s+(\d{2,5})\b",
        text,
        flags=re.I,
    ):
        _add_port(m.group(1))

    # port 31337 / ports: 80, 443, 31337 / tcp/31337 / TCP 31337
    for m in re.finditer(
        r"\b(?:ports?|tcp|udp)\s*[#:=/\-]?\s*(\d{1,5}(?:\s*,\s*\d{1,5})*)\b",
        text,
        flags=re.I,
    ):
        for part in re.split(r"\s*,\s*", m.group(1)):
            _add_port(part)

    return hosts[:8], ports[:32]


def harden_nmap_command(command: str) -> str:
    """Make agent nmap reliable through Docker/VPN/SOCKS.

    Force ``-Pn`` and TCP connect ``-sT`` (SYN scans often lie in containers).
    Rewrite explicit ``-sS`` to ``-sT``. Does **not** rewrite port ranges —
    custom / full-port scans stay intact on every routing path.
    """
    import re

    # Only when nmap is invoked as a command (not `echo nmap`).
    if not (re.match(r"nmap\b", command.lstrip()) or re.search(r"[\n;|&]\s*nmap\b", command)):
        return command

    # SYN needs raw sockets; rewrite to connect scan. Leave -sU / -sV / etc.
    command = re.sub(r"(?<!\S)-sS\b", "-sT", command)

    # Real scan types only — not -sV/-sC (version/scripts).
    has_scan = re.search(r"(?<!\S)-s(?:T|S|A|W|M|U|Y|Z|O|N|F|X)\b", command)
    has_pn = re.search(r"(?<!\S)-Pn\b", command)
    list_or_ping = re.search(r"(?<!\S)-s[nL]\b", command)

    insert: list[str] = []
    if not has_pn and not list_or_ping:
        insert.append("-Pn")
    if not has_scan and not list_or_ping:
        insert.append("-sT")
    if not insert:
        return command

    flags = " ".join(insert)

    def _repl_head(m: re.Match[str]) -> str:
        return f"{m.group(1)}nmap {flags}"

    if re.match(r"nmap\b", command.lstrip()):
        leading = command[: len(command) - len(command.lstrip())]
        return leading + re.sub(r"nmap\b", f"nmap {flags}", command.lstrip(), count=1)
    return re.sub(r"([\n;|&]\s*)nmap\b", _repl_head, command, count=1)


def harden_hosts_edit_command(command: str) -> str:
    """Rewrite in-place ``sed -i … /etc/hosts`` to a temp-file rewrite.

    Docker bind-mounts ``/etc/hosts``; ``sed -i`` fails with
    ``Device or resource busy``.
    """
    import re

    if "/etc/hosts" not in command or "sed" not in command:
        return command
    if "ctf-hosts-add" in command:
        return command

    # Match a sed -i (optional suffix) … /etc/hosts invocation
    pattern = re.compile(
        r"(?P<head>^|[\n;|&]\s*)sed\s+-i\S*\s+(?P<body>.+?)\s+/etc/hosts\b",
    )

    def _rewrite(m: re.Match[str]) -> str:
        head = m.group("head")
        body = m.group("body").strip()
        # body is typically "'s/foo/bar/'" or similar sed script + optional args
        return (
            f'{head}tmp=$(mktemp) && sed {body} /etc/hosts > "$tmp" '
            f'&& cat "$tmp" > /etc/hosts && rm -f "$tmp"'
        )

    return pattern.sub(_rewrite, command, count=1)
