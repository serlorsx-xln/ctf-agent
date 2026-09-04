"""First-run setup readiness probe (no Docker required for unit bits)."""

from pathlib import Path

from backend.sandbox.setup_ready import probe_setup_status
from backend.tool_router import PACK_SPECS


def _materialize_pack_cache(root: Path, pack_id: str) -> None:
    """Create ``.ready`` + PACK_SPECS path sentinels so ``_pack_cache_is_ready`` passes."""
    cache = root / pack_id / "arm64"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / ".ready").write_text("ok\n", encoding="utf-8")
    spec = PACK_SPECS.get(pack_id)
    if not spec:
        return
    for p in spec.paths:
        dest = cache / p.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text("sentinel\n", encoding="utf-8")


def test_probe_ready_with_l0_even_if_packs_missing(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr("backend.sandbox.setup_ready._docker_ok", lambda: True)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready._docker_image_exists",
        lambda tag: tag == "ctf-sandbox-core",
    )
    monkeypatch.setattr("backend.sandbox.setup_ready._container_dns_ok", lambda: True)
    monkeypatch.setattr("backend.sandbox.packs._pack_cache_is_ready", lambda pack_id: False)
    st = probe_setup_status()
    assert st.ready is True
    assert st.core_image is True
    assert "pwn" in st.packs_missing
    assert "crypto" in st.packs_missing
    assert "on demand" in st.message
    assert "Pack caches not baked" not in st.message


def test_probe_reports_missing_without_docker(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready._docker_ok",
        lambda: False,
    )
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id / "arm64",
    )
    st = probe_setup_status(required_packs=["web", "pwn"])
    assert st.ready is False
    assert st.docker_ok is False
    assert "Docker" in st.message


def test_probe_ready_when_core_and_packs(monkeypatch, tmp_path: Path):
    """Gate readiness must match solve-time ``_pack_cache_is_ready`` (not .ready alone)."""
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr("backend.sandbox.setup_ready._docker_ok", lambda: True)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready._docker_image_exists",
        lambda tag: tag == "ctf-sandbox-core",
    )
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id / "arm64",
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_cache_stale",
        lambda pack_id: False,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_cache_incomplete",
        lambda pack_id: False,
    )
    monkeypatch.setattr("backend.sandbox.setup_ready._container_dns_ok", lambda: True)
    for pack in ("web", "pwn"):
        _materialize_pack_cache(tmp_path, pack)
    st = probe_setup_status(required_packs=["web", "pwn"])
    assert st.ready is True
    assert st.core_image is True
    assert set(st.packs_ready) == {"web", "pwn"}


def test_probe_not_ready_when_pack_paths_missing(monkeypatch, tmp_path: Path):
    """``.ready`` without PACK_SPECS path sentinels must not pass the gate."""
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr("backend.sandbox.setup_ready._docker_ok", lambda: True)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready._docker_image_exists",
        lambda tag: tag == "ctf-sandbox-core",
    )
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id / "arm64",
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_cache_stale",
        lambda pack_id: False,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_cache_incomplete",
        lambda pack_id: False,
    )
    d = tmp_path / "pwn" / "arm64"
    d.mkdir(parents=True)
    (d / ".ready").write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr("backend.sandbox.setup_ready._container_dns_ok", lambda: True)
    st = probe_setup_status(required_packs=["pwn"])
    assert st.ready is True
    assert st.packs_missing == ["pwn"]
    assert "on demand" in st.message


class _Proc:
    def __init__(self, rc: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_image_refs_add_latest_when_untagged():
    from backend.sandbox.setup_ready import _image_refs

    assert _image_refs("ctf-sandbox-core") == (
        "ctf-sandbox-core:latest",
        "ctf-sandbox-core",
    )
    assert _image_refs("ctf-sandbox-core:latest") == ("ctf-sandbox-core:latest",)


def test_docker_image_exists_via_images_q_when_inspect_misses(monkeypatch):
    """Engine 29: untagged inspect fails; ``docker images -q`` still sees :latest."""
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def fake(args, *, timeout_s):  # noqa: ARG001
        if args[:2] == ["images", "-q"]:
            return _Proc(0, b"20fb29c7431d\n")
        return _Proc(1, b"", b"Error response from daemon: No such image: ctf-sandbox-core\n")

    monkeypatch.setattr("backend.sandbox.setup_ready._docker_run", fake)
    from backend.sandbox.setup_ready import _docker_image_exists

    assert _docker_image_exists("ctf-sandbox-core") is True


def test_docker_image_exists_false_when_absent(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def fake(args, *, timeout_s):  # noqa: ARG001
        return _Proc(1, b"", b"No such image")

    monkeypatch.setattr("backend.sandbox.setup_ready._docker_run", fake)
    from backend.sandbox.setup_ready import _docker_image_exists

    assert _docker_image_exists("no-such-image") is False


def test_docker_image_exists_retries_timeout_then_lists(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    n = {"i": 0}

    def fake(args, *, timeout_s):  # noqa: ARG001
        n["i"] += 1
        if n["i"] < 3:
            return None
        if args[:2] == ["images", "-q"]:
            return _Proc(0, b"abc\n")
        return _Proc(1)

    monkeypatch.setattr("backend.sandbox.setup_ready._docker_run", fake)
    from backend.sandbox.setup_ready import _docker_image_exists

    assert _docker_image_exists("ctf-sandbox-core") is True


def test_gate_install_bakes_full_packs_but_skips_blutter():
    import inspect

    from backend.sandbox.setup_ready import run_gate_install

    params = inspect.signature(run_gate_install).parameters
    assert params["skip_warm_runtime"].default is False
    assert params["skip_blutter_vm"].default is True


def test_dns_probe_advisory_does_not_block_ready(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr("backend.sandbox.setup_ready._docker_ok", lambda: True)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready._docker_image_exists",
        lambda tag: tag == "ctf-sandbox-core",
    )
    monkeypatch.setattr("backend.sandbox.setup_ready._container_dns_ok", lambda: False)
    st = probe_setup_status(required_packs=[])
    assert st.ready is True
    assert st.dns_ok is False
    assert "DNS" in st.message
