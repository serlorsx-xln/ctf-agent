"""Host SOCKS / lab probe helpers used by DockerSandbox calibration."""

from __future__ import annotations

_DEFAULT_PROBE_PORTS: tuple[int, ...] = (
    22,
    80,
    443,
    445,
    3389,
    5985,
    8080,
    8443,
    8000,
    3000,
    1,
    65535,
)


def _lab_probe_script(hosts: list[str], ports: list[int], ok_token: str) -> str:
    """TCP probe: open OR connection-refused both mean the lab route works."""
    hosts_py = ",".join(repr(h) for h in hosts)
    ports_py = ",".join(str(p) for p in ports)
    fail_token = ok_token.replace("OK", "FAIL")
    return f"""
import errno, socket
hosts=[{hosts_py}]
ports=[{ports_py}]
for h in hosts:
    for p in ports:
        try:
            s=socket.create_connection((h,p), timeout=4)
            s.close()
            print({ok_token!r}, h, p, 'open')
            raise SystemExit(0)
        except ConnectionRefusedError:
            print({ok_token!r}, h, p, 'refused')
            raise SystemExit(0)
        except OSError as e:
            if getattr(e, 'errno', None) in (errno.ECONNREFUSED, 111, 61):
                print({ok_token!r}, h, p, 'refused')
                raise SystemExit(0)
        except Exception:
            pass
print({fail_token!r})
"""
