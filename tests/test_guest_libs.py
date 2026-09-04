"""Guest-lib inventory helpers (no Docker)."""

from backend.sandbox.guest_libs import flatten_guest_lib_gaps


def test_flatten_guest_lib_gaps_stable() -> None:
    lines = flatten_guest_lib_gaps(
        {
            "mobile": ["/usr/x86_64-linux-gnu/lib/libstdc++.so.6"],
            "pwn": ["image not installed"],
        }
    )
    assert lines[0] == "pwn:image not installed"
    assert lines[1].startswith("mobile:")
