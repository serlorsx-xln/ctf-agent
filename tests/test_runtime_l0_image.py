"""Runtime L0 selection from prefetch packs."""

from backend.tool_router import resolve_runtime_l0_image


def test_pwn_prefetch_uses_baked_runtime():
    assert resolve_runtime_l0_image(["pwn"]) == "ctf-sandbox-pwn"
    assert resolve_runtime_l0_image(["ghidra", "pwn"]) == "ctf-sandbox-pwn"
    assert resolve_runtime_l0_image(["pwn", "ghidra"]) == "ctf-sandbox-pwn"


def test_crypto_prefetch_stays_on_core():
    assert resolve_runtime_l0_image(["crypto"]) == "ctf-sandbox-core"
    assert resolve_runtime_l0_image(["mobile"]) == "ctf-sandbox-core"


def test_custom_l0_not_overridden():
    assert resolve_runtime_l0_image(["pwn"], "my-custom-image") == "my-custom-image"
