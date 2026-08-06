from backend.sandbox import parse_challenge_network_hints


def test_parses_lab_fqdn_without_ip():
    hosts, ports = parse_challenge_network_hints(
        "Connect to dc1.ping.htb for LDAP\nWinRM on port 5985\n"
    )
    assert "dc1.ping.htb" in hosts
    assert 5985 in ports


def test_parses_nc_line_host():
    hosts, ports = parse_challenge_network_hints("nc challenge.htb 1337\n")
    assert "challenge.htb" in hosts
    assert 1337 in ports
