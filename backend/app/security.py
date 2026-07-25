"""Input-boundary helpers for the optional network-facing deployment mode."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


LOCAL_HOSTNAMES = {
    "localhost", "localhost.localdomain", "host.docker.internal",
    "gateway.docker.internal", "metadata.google.internal",
}
URL_IN_TEXT = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
AMBIGUOUS_IPV4 = re.compile(
    r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)"
    r"(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}$"
)


def validate_source_url(value: str, *, allow_private: bool = False) -> str:
    value = value.strip()
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("source URL contains invalid characters")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("source URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in a source URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source URL has an invalid port") from exc
    host = parsed.hostname.rstrip(".").lower()
    if not host or "%" in host:
        raise ValueError("source URL has an invalid host")
    if not allow_private:
        if port not in {None, 80, 443}:
            raise ValueError("only standard HTTP and HTTPS source ports are enabled")
        if host in LOCAL_HOSTNAMES or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("private-network source URLs are disabled")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is None and AMBIGUOUS_IPV4.fullmatch(host):
            # libc accepts legacy one-, two-, and three-part IPv4 forms plus
            # octal/hex components (for example 127.1 and 0177.0.0.1).
            # Reject them rather than letting a later resolver reinterpret
            # what the admission check saw as a hostname.
            raise ValueError("private-network source URLs are disabled")
        if address is None and "." not in host:
            # Docker/cluster services commonly use single-label DNS names such
            # as "api" and "redis". They are never public Internet sources.
            raise ValueError("private-network source URLs are disabled")
        if address and not address.is_global:
            raise ValueError("private, loopback and link-local source URLs are disabled")
    return value


def redact_url(value: str) -> str:
    """Return only a URL's origin for display, logs, and durable metadata.

    Paths are not generally safe: Zoom email links, private CDN shares, and
    other providers place bearer capabilities in path segments rather than in
    query parameters. The full source is retained only in its protected source
    field/auth context; provenance uses a digest when it needs identity.
    """
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return "<invalid-url>"
        return urlunsplit((parsed.scheme, host, "", "", ""))
    except Exception:
        return "<invalid-url>"


def redact_urls(value: str) -> str:
    """Remove credentials/query/fragment data from URLs embedded in text."""
    return URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), value)
