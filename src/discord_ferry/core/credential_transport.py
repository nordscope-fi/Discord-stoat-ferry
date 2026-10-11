"""Destination policy for requests that carry Stoat credentials."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


def credential_transport_allowed(url: str) -> bool:
    """Return whether a credential-bearing request may use *url*."""
    if any(
        character.isspace() or ord(character) <= 0x1F or ord(character) == 0x7F for character in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if hostname is None or any(character.isspace() for character in hostname):
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    hostname = hostname.lower()
    local_hostname = hostname.rstrip(".")
    if local_hostname == "localhost" or local_hostname.endswith(".localhost"):
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_private or address.is_loopback
