"""Artemis control-plane daemon — NDJSON over a Unix socket.

A long-lived supervisor process that owns session state, swarm subprocess
lifecycle, and dialog handshakes (flag-confirm / flags-ask). The TUI connects
as a client; the swarm subprocess also connects as a client to emit usage and
dialog requests. Files in ``~/.cache/artemis/`` are demoted to crash-recovery
hydration and debug only — they are no longer the control channel.

See ``docs`` / plan for the protocol.
"""
