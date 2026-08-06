"""Prefetch ML pack from TF/torch imports and challenge text."""

from pathlib import Path

from backend.tool_router import detect_packs, infer_pack_from_command, infer_pack_from_failure


def test_detect_ml_from_tensorflow_import(tmp_path: Path) -> None:
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "anc.py").write_text(
        "#!/usr/bin/env python3\nimport tensorflow as tf\nimport numpy as np\n",
        encoding="utf-8",
    )
    (tmp_path / "challenge.txt").write_text("crypto challenge\n", encoding="utf-8")
    assert "ml" in detect_packs(tmp_path)


def test_detect_ml_from_challenge_text(tmp_path: Path) -> None:
    (tmp_path / "challenge.txt").write_text(
        "Tags: ml, crypto\nAdversarial neural cryptography with TensorFlow.\n",
        encoding="utf-8",
    )
    assert "ml" in detect_packs(tmp_path)


def test_infer_pack_tensorflow_import() -> None:
    assert infer_pack_from_command("python3 -c 'import tensorflow'") == "ml"
    assert (
        infer_pack_from_failure(
            "python3 -c 'import tensorflow'",
            "ModuleNotFoundError: No module named 'tensorflow'",
        )
        == "ml"
    )
