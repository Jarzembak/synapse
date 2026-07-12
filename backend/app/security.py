"""Input-boundary helpers for the optional network-facing deployment mode."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


LOCAL_HOSTNAMES = {
    "localhost", "localhost.localdomain", "host.docker.internal",
    "gateway.docker.internal", "metadata.google.internal",
}


def validate_source_url(value: str, *, allow_private: bool = False) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be an absolute http:// or https:// URL")
    host = parsed.hostname.rstrip(".").lower()
    if not allow_private:
        if host in LOCAL_HOSTNAMES or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("private-network source URLs are disabled")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError("private, loopback and link-local source URLs are disabled")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in a source URL")
    return value


def redact_url(value: str) -> str:
    """Remove credentials and signed query/fragment data before logging."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return "<invalid-url>"
