"""Protocol-level tests for the Artemis daemon NDJSON envelope."""

from __future__ import annotations

import pytest

from backend.daemon import protocol


def test_make_message_envelope_shape() -> None:
    msg = protocol.make_message(type="status", id="abc", session="s1", extra=1)
    assert msg["v"] == protocol.PROTOCOL_VERSION
    assert msg["type"] == "status"
    assert msg["id"] == "abc"
    assert msg["session"] == "s1"
    assert msg["extra"] == 1


def test_encode_decode_roundtrip() -> None:
    msg = protocol.make_message(type="hello", id="h1", role="tui")
    enc = protocol.encode(msg)
    assert enc.endswith(b"\n")
    dec = protocol.decode(enc)
    assert dec == msg


def test_decode_rejects_wrong_version() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b'{"v": 2, "type": "x"}')


def test_decode_rejects_non_object() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b"[1, 2, 3]")


def test_decode_rejects_missing_type() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b'{"v": 1, "id": "x"}')


def test_decode_rejects_bad_json() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b"not json at all")


def test_make_error_envelope() -> None:
    err = protocol.make_error("req-1", "boom")
    assert err["type"] == "error"
    assert err["ok"] is False
    assert err["error"] == "boom"
    assert err["id"] == "req-1"


def test_new_id_unique() -> None:
    ids = {protocol.new_id() for _ in range(100)}
    assert len(ids) == 100
