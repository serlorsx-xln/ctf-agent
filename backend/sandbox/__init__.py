"""Sandbox package facade — public imports stay ``from backend.sandbox import ...``."""

from __future__ import annotations

from backend.sandbox.container import DockerSandbox
from backend.sandbox.docker_client import cleanup_orphan_containers, configure_semaphore
from backend.sandbox.harden import (
    harden_hosts_edit_command,
    harden_nmap_command,
    parse_challenge_network_hints,
)
from backend.sandbox.packs import (
    _acquire_pack_flock,
    _pack_cache_is_ready,
    _pack_cache_lock,
    _release_pack_flock,
)

__all__ = [
    "DockerSandbox",
    "cleanup_orphan_containers",
    "configure_semaphore",
    "harden_hosts_edit_command",
    "harden_nmap_command",
    "parse_challenge_network_hints",
    # Test / internal helpers re-exported for compatibility
    "_acquire_pack_flock",
    "_pack_cache_is_ready",
    "_pack_cache_lock",
    "_release_pack_flock",
]
