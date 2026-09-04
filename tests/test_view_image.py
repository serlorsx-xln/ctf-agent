"""view_image accepts path like the other file tools (live garden hung on filename-only)."""

from __future__ import annotations

import pytest

from backend.agents.codex_solver import SANDBOX_TOOLS
from backend.agents.cursor_solver import _tool_args_preview
from backend.tools.core import VIEW_IMAGE_INPUT_SCHEMA, do_view_image, view_image_arg

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class _FakeSandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def read_file_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def test_view_image_arg_accepts_path_like_other_file_tools() -> None:
    assert view_image_arg({"path": "/challenge/distfiles/garden.jpg"}) == (
        "/challenge/distfiles/garden.jpg"
    )
    assert view_image_arg({"filename": "garden.jpg"}) == "garden.jpg"
    assert view_image_arg({"path": "garden.jpg", "filename": ""}) == "garden.jpg"
    assert view_image_arg({}) == ""


@pytest.mark.asyncio
async def test_do_view_image_empty_arg_is_explicit() -> None:
    out = await do_view_image(_FakeSandbox({}), "")
    assert "filename or path" in out


@pytest.mark.asyncio
async def test_do_view_image_loads_jpeg_from_absolute_path() -> None:
    path = "/challenge/distfiles/garden.jpg"
    data, mime = await do_view_image(_FakeSandbox({path: JPEG}), path)
    assert mime == "image/jpeg"
    assert data[:3] == b"\xff\xd8\xff"


def test_view_image_schema_accepts_path() -> None:
    props = VIEW_IMAGE_INPUT_SCHEMA["properties"]
    assert "filename" in props
    assert "path" in props
    assert "required" not in VIEW_IMAGE_INPUT_SCHEMA
    codex = next(t for t in SANDBOX_TOOLS if t["name"] == "view_image")
    assert codex["inputSchema"] is VIEW_IMAGE_INPUT_SCHEMA


def test_cursor_preview_uses_path() -> None:
    assert (
        _tool_args_preview("view_image", {"path": "/challenge/distfiles/garden.jpg"})
        == "/challenge/distfiles/garden.jpg"
    )
