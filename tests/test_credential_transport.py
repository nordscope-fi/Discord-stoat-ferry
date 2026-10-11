"""Credential destination policy tests."""

from __future__ import annotations

import pytest

from discord_ferry.core.credential_transport import credential_transport_allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://api.stoat.example",
        "https://8.8.8.8/api",
        "http://localhost/api",
        "http://localhost./api",
        "http://worker.localhost/api",
        "http://worker.localhost./api",
        "http://127.0.0.1/api",
        "http://10.0.0.1/api",
        "http://192.168.1.1/api",
        "http://169.254.1.1/api",
        "http://192.0.2.1/api",
        "http://198.51.100.1/api",
        "http://203.0.113.1/api",
        "http://240.0.0.1/api",
        "http://[::1]/api",
        "http://[fc00::1]/api",
        "http://[fe80::1]/api",
        "http://[2001:db8::1]/api",
    ],
)
def test_credential_transport_allows_approved_destinations(url: str) -> None:
    assert credential_transport_allowed(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://stoat.example/api",
        "http://8.8.8.8/api",
        "http://100.64.0.1/api",
        "http://[2001:4860:4860::8888]/api",
        "http://192.168.1.1./api",
        "http://local host/api",
        "http://local\nhost/api",
        "http://local\thost/api",
        "http:///api",
        "http://[::1/api",
        "http://localhost:not-a-port/api",
        "ftp://localhost/api",
        "localhost/api",
        "",
    ],
)
def test_credential_transport_rejects_disallowed_destinations(url: str) -> None:
    assert credential_transport_allowed(url) is False


@pytest.mark.parametrize("control", [*range(0x20), 0x7F])
def test_credential_transport_rejects_leading_control_bytes(control: int) -> None:
    url = f"{chr(control)}http://localhost/api"

    assert credential_transport_allowed(url) is False
