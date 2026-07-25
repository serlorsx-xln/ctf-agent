"""NDJSON protocol for the Artemis daemon control plane.

Every line on the socket is one JSON object (the ``envelope``):

    {"v": 1, "id": "<uuid>|null", "type": "<type>", "session": "<sid>|null", ...}

- ``v``      protocol version (server rejects ``v != 1``).
- ``id``     correlation id. Clients set it on requests; the server echoes it
             on the matching response. Push events use ``null``.
- ``session`` OpenCode session id for routing (dialogs, state).
- ``type``   discriminator. The remaining fields depend on ``type``.

This module is deliberately I/O-free: it only (de)serializes and validates the
envelope so both the server and the tests can use it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

PROTOCOL_VERSION = 1

#: Roles advertised in the initial ``hello`` message.
ROLE_TUI = "tui"
ROLE_SWARM = "swarm"
# Fire-and-forget publisher from the swarm process (usage snapshots +
# session_refresh). Must NOT run the full swarm/TUI dialog loop — disconnect
# would otherwise cancel pending flag confirms.
ROLE_USAGE = "usage"


def new_id() -> str:
    return uuid.uuid4().hex


def make_message(
    *,
    type: str,
    id: str | None = None,
    session: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build an envelope dict."""
    msg: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": type}
    msg["id"] = id
    msg["session"] = session
    msg.update(fields)
    return msg


def make_response(
    *,
    req_id: str | None,
    type: str,
    ok: bool = True,
    session: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    msg = make_message(type=type, id=req_id, session=session, ok=ok, **fields)
    return msg


def make_error(req_id: str | None, message: str, *, session: str | None = None) -> dict[str, Any]:
    return make_message(type="error", id=req_id, session=session, ok=False, error=message)


def encode(msg: dict[str, Any]) -> bytes:
    """Serialize a message to a single NDJSON line (no trailing content)."""
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class ProtocolError(Exception):
    pass


def decode(line: bytes) -> dict[str, Any]:
    """Decode one NDJSON line into a validated envelope dict.

    Raises ``ProtocolError`` on malformed JSON or wrong version.
    """
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError(f"invalid json: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("envelope must be a JSON object")
    v = msg.get("v")
    if v != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {v!r}")
    if "type" not in msg or not isinstance(msg["type"], str):
        raise ProtocolError("missing 'type'")
    return msg
