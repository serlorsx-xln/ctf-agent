"""Paste → materialize challenge workspace."""

from __future__ import annotations

from pathlib import Path

from backend.challenge import (
    is_challenge_dir,
    load_challenge,
    materialize_challenge,
    resolve_load_target,
    slugify_challenge_name,
)


def test_slugify() -> None:
    assert "hello" in slugify_challenge_name("Hello World CTF\nmore")
    assert slugify_challenge_name("") == "paste"


def test_materialize_writes_challenge_txt(tmp_path: Path) -> None:
    body = "Web challenge\n\nhttps://lab.example:1337/\n\nflags_required: 1\n"
    dest = materialize_challenge(description=body, cache_root=tmp_path)
    assert (dest / "challenge.txt").is_file()
    assert is_challenge_dir(dest)
    meta = load_challenge(dest)
    assert meta.flags_required == 1
    assert "lab.example" in (meta.connection_info or meta.description)


def test_materialize_copies_attachments(tmp_path: Path) -> None:
    blob = tmp_path / "chal.bin"
    blob.write_bytes(b"MZ")
    dest = materialize_challenge(
        description="Bin challenge\n",
        attachments=[str(blob)],
        cache_root=tmp_path / "cache",
    )
    assert (dest / "distfiles" / "chal.bin").is_file()


def test_resolve_path_only_uses_dir(tmp_path: Path) -> None:
    chal = tmp_path / "real"
    chal.mkdir()
    (chal / "challenge.txt").write_text("from path\n", encoding="utf-8")
    got = resolve_load_target(path=str(chal))
    assert got == chal.resolve()


def test_resolve_dir_plus_paste_keeps_nc(tmp_path: Path, monkeypatch) -> None:
    """Folder + first-prompt paste (often nc) must materialize challenge.txt."""
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    chal = tmp_path / "glass-ff62bfb33258"
    chal.mkdir()
    (chal / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (chal / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    got = resolve_load_target(
        path=str(chal),
        prompt="Glass esolang\nnc chall.example 31337\n",
    )
    assert got != chal.resolve()
    body = (got / "challenge.txt").read_text(encoding="utf-8")
    assert "nc chall.example 31337" in body
    assert (got / "distfiles" / "Dockerfile").is_file()
    assert (got / "distfiles" / "compose.yaml").is_file()


def test_resolve_dir_plus_paste_merges_existing_desc(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    chal = tmp_path / "real"
    chal.mkdir()
    (chal / "challenge.txt").write_text("from path\n", encoding="utf-8")
    (chal / "blob.bin").write_bytes(b"MZ")
    got = resolve_load_target(path=str(chal), prompt="nc 1.2.3.4 9999")
    body = (got / "challenge.txt").read_text(encoding="utf-8")
    assert "from path" in body
    assert "nc 1.2.3.4 9999" in body
    assert (got / "distfiles" / "blob.bin").is_file()


def test_resolve_prompt_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    got = resolve_load_target(prompt="Pasted challenge\nnc 1.2.3.4 9999\n")
    assert got.is_dir()
    assert (got / "challenge.txt").read_text(encoding="utf-8").startswith("Pasted")


def test_resolve_path_with_attachments_copies_multi_folder(tmp_path: Path) -> None:
    """Multi-folder: path=folder1 + attachments=[folder2, file] → all copied in."""
    main = tmp_path / "main_chal"
    main.mkdir()
    (main / "challenge.txt").write_text("main\n", encoding="utf-8")

    extra_dir = tmp_path / "extra_dir"
    (extra_dir / "nested.txt").parent.mkdir(parents=True, exist_ok=True)
    extra_dir.mkdir(exist_ok=True)
    (extra_dir / "nested.txt").write_text("nested", encoding="utf-8")

    extra_file = tmp_path / "capture.pcap"
    extra_file.write_bytes(b"\xd4\xc3\xb2\xa1")

    got = resolve_load_target(
        path=str(main),
        attachments=[str(extra_dir), str(extra_file)],
    )
    assert got == main.resolve()
    # Both the extra folder and the file must land in distfiles.
    assert (got / "distfiles" / "extra_dir" / "nested.txt").is_file()
    assert (got / "distfiles" / "capture.pcap").is_file()


def test_resolve_file_primary_materializes(tmp_path: Path, monkeypatch) -> None:
    """Single file path → cache challenge with file in distfiles (not 'not a directory')."""
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    apk = tmp_path / "PWNKnight.apk"
    apk.write_bytes(b"PK\x03\x04")
    got = resolve_load_target(path=str(apk))
    assert got.is_dir()
    assert (got / "distfiles" / "PWNKnight.apk").is_file()
    assert "PWNKnight" in (got / "challenge.txt").read_text(encoding="utf-8")


def test_resolve_multi_file_and_folder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    a = tmp_path / "a.bin"
    a.write_bytes(b"AA")
    b = tmp_path / "b.bin"
    b.write_bytes(b"BB")
    folder = tmp_path / "extra"
    folder.mkdir()
    (folder / "note.txt").write_text("hi", encoding="utf-8")
    got = resolve_load_target(
        path=str(a),
        attachments=[str(b), str(folder)],
        prompt="Mobile chal\nhttps://lab.example/chal\n",
    )
    assert (got / "distfiles" / "a.bin").is_file()
    assert (got / "distfiles" / "b.bin").is_file()
    assert (got / "distfiles" / "extra" / "note.txt").is_file()
    body = (got / "challenge.txt").read_text(encoding="utf-8")
    assert "https://lab.example/chal" in body  # web link stays in paste — not fetched


def test_resolve_missing_path_message(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "missing"
    try:
        resolve_load_target(path=str(missing))
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as e:
        assert "path not found" in str(e).lower()
        assert "not a directory" not in str(e).lower()


def test_resolve_paste_with_url_no_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    url = "https://ctf.example/web/challenge"
    got = resolve_load_target(prompt=f"Find the flag\n{url}\n")
    assert url in (got / "challenge.txt").read_text(encoding="utf-8")
