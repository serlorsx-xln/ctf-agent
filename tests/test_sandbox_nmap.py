"""Unit tests for nmap hardening + challenge network hints (no Docker)."""

from backend.sandbox import (
    harden_hosts_edit_command,
    harden_nmap_command,
    parse_challenge_network_hints,
)


def test_injects_pn_and_st():
    out = harden_nmap_command("nmap 10.0.0.1")
    assert out.startswith("nmap -Pn -sT ")
    assert "10.0.0.1" in out


def test_rewrites_syn_to_connect():
    out = harden_nmap_command("nmap -sS -p 80,443 10.0.0.1")
    assert "-sS" not in out
    assert "-sT" in out
    assert "-Pn" in out
    assert "-p 80,443" in out


def test_preserves_udp_scan_type():
    out = harden_nmap_command("nmap -sU -p 53 10.0.0.1")
    assert "-sU" in out
    assert "-Pn" in out
    assert "-sT" not in out


def test_ignores_echo_nmap():
    assert harden_nmap_command("echo nmap -p-") == "echo nmap -p-"


def test_pipeline_nmap_hardened():
    out = harden_nmap_command("true; nmap 10.0.0.1 | head")
    assert "; nmap -Pn -sT 10.0.0.1 |" in out


def test_keeps_full_port_sweep():
    """Never amputate -p- — custom ports live outside top-N."""
    out = harden_nmap_command("nmap -p- 10.0.0.1")
    assert "-p-" in out
    assert "--top-ports" not in out


def test_keeps_1_65535():
    out = harden_nmap_command("nmap -p 1-65535 10.0.0.1")
    assert "1-65535" in out


def test_keeps_focused_custom_port():
    out = harden_nmap_command("nmap -p 31337 10.0.0.1")
    assert "-p 31337" in out


def test_parse_ip_and_host_port():
    hosts, ports = parse_challenge_network_hints("Target 10.129.1.5:31337 and also 192.168.0.2\n")
    assert hosts == ["10.129.1.5", "192.168.0.2"]
    assert 31337 in ports


def test_parse_port_keywords():
    hosts, ports = parse_challenge_network_hints(
        "Box at 10.10.10.10\nConnect to port 4444 or tcp/9001\nports: 80, 443, 1337\n"
    )
    assert hosts == ["10.10.10.10"]
    assert ports == [4444, 9001, 80, 443, 1337]


def test_parse_skips_bare_numbers_without_port_context():
    _hosts, ports = parse_challenge_network_hints(
        "flags_required: 2\nSubmit 32 hex characters\nTarget 10.0.0.1\n"
    )
    assert ports == []


def test_hosts_sed_i_rewritten_to_tempfile():
    out = harden_hosts_edit_command("sed -i 's/old/new/' /etc/hosts")
    assert "sed -i" not in out
    assert "tmp=$(mktemp)" in out
    assert 'cat "$tmp" > /etc/hosts' in out


def test_hosts_non_sed_untouched():
    cmd = "echo '10.0.0.1 dc.htb' | tee -a /etc/hosts"
    assert harden_hosts_edit_command(cmd) == cmd
