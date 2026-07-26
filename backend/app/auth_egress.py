"""Filtering HTTP forward proxy for the disposable authentication browser.

The browser used to collect media-site authentication has no reason to reach
Synapse, Docker services, cloud metadata endpoints, or the host's private
network.  This small, dependency-free proxy is the egress boundary for that
browser:

* destinations must use port 80 or 443;
* DNS answers are admitted only when every address is public and globally
  routable;
* the upstream socket is connected to the already-resolved numeric address, so
  a second DNS lookup cannot rebind the destination;
* request headers, setup time, and connection lifetime are bounded; and
* logs contain only the method and normalized host/port, never URLs, headers,
  cookies, credentials, paths, queries, or exception strings.

It supports the two forms emitted by configured web browsers: absolute-form
HTTP requests and HTTPS ``CONNECT`` tunnels.  Run it with
``python -m app.auth_egress``.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit


logger = logging.getLogger(__name__)

ALLOWED_DESTINATION_PORTS = frozenset({80, 443})
HEALTH_PATH = "/.well-known/synapse-auth-egress-health"
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HTTP_TOKEN = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_LOCAL_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "host.docker.internal",
        "gateway.docker.internal",
        "metadata.google.internal",
    }
)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal")
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_PLATFORM_VIRTUAL_IPS = frozenset(
    {
        # Azure WireServer/platform virtual IP.  It is classified as globally
        # routable by generic IP libraries but is special inside Azure guests.
        ipaddress.ip_address("168.63.129.16"),
    }
)

_STATUS_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class ProxyRejection(Exception):
    """A safe, classified failure that may be returned to a proxy client."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    max_header_bytes: int = 32 * 1024
    max_header_line_bytes: int = 8 * 1024
    max_header_count: int = 100
    header_timeout_seconds: float = 10.0
    dns_timeout_seconds: float = 10.0
    connect_timeout_seconds: float = 10.0
    connection_timeout_seconds: float = 10 * 60.0
    drain_timeout_seconds: float = 30.0
    max_connections: int = 64

    def __post_init__(self) -> None:
        if not 0 <= self.bind_port <= 65_535:
            raise ValueError("bind port must be between 0 and 65535")
        if not 1_024 <= self.max_header_bytes <= 1024 * 1024:
            raise ValueError("max header bytes must be between 1024 and 1048576")
        if not 256 <= self.max_header_line_bytes <= self.max_header_bytes:
            raise ValueError("max header line bytes is outside the header limit")
        if not 1 <= self.max_header_count <= 1_000:
            raise ValueError("max header count must be between 1 and 1000")
        for name, value in (
            ("header timeout", self.header_timeout_seconds),
            ("DNS timeout", self.dns_timeout_seconds),
            ("connect timeout", self.connect_timeout_seconds),
            ("connection timeout", self.connection_timeout_seconds),
            ("drain timeout", self.drain_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if not 1 <= self.max_connections <= 10_000:
            raise ValueError("max connections must be between 1 and 10000")


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """A vetted numeric destination.  ``ip`` is never a hostname."""

    family: socket.AddressFamily
    ip: str
    port: int


@dataclass(frozen=True, slots=True)
class Header:
    name: bytes
    value: bytes


@dataclass(frozen=True, slots=True)
class ProxyRequest:
    method: str
    target: str
    version: str
    headers: tuple[Header, ...]


@dataclass(frozen=True, slots=True)
class Destination:
    host: str
    port: int
    origin_target: str | None


Resolver = Callable[
    [str, int, float], Awaitable[tuple[ResolvedAddress, ...]]
]
Connector = Callable[
    [Sequence[ResolvedAddress], float],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


def _public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address is safe as an Internet egress destination."""
    if (
        address in _PLATFORM_VIRTUAL_IPS
        or not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False

    if isinstance(address, ipaddress.IPv6Address):
        # IPv4 transition forms need their embedded address checked as well.
        # This closes common IPv4-mapped and DNS64 SSRF bypasses.
        if address.ipv4_mapped is not None:
            return _public_ip(address.ipv4_mapped)
        if address.sixtofour is not None and not _public_ip(address.sixtofour):
            return False
        if address.teredo is not None:
            # Teredo relays obscure the effective IPv4 endpoint and are not
            # needed by the authentication browser.
            return False
        if address in _NAT64_WELL_KNOWN:
            embedded = ipaddress.IPv4Address(address.packed[-4:])
            return _public_ip(embedded)
        if address in _NAT64_LOCAL:
            # RFC 8215 permits variable embedding layouts under this prefix.
            return False

    return True


def _normalize_host(
    host: str,
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """Normalize a host and reject names that can only be local/service names."""
    value = host.strip().rstrip(".").lower()
    if not value or "\x00" in value or "%" in value:
        raise ProxyRejection(403, "destination_denied")

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None

    if address is not None:
        if not _public_ip(address):
            raise ProxyRejection(403, "destination_denied")
        return address.compressed, address

    try:
        ascii_host = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ProxyRejection(403, "destination_denied") from exc

    if (
        len(ascii_host) > 253
        or "." not in ascii_host
        or ascii_host in _LOCAL_NAMES
        or ascii_host.endswith(_LOCAL_SUFFIXES)
    ):
        raise ProxyRejection(403, "destination_denied")
    labels = ascii_host.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ProxyRejection(403, "destination_denied")
    return ascii_host, None


async def _getaddrinfo(host: str, port: int) -> list[tuple]:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


async def resolve_public_destination(
    host: str,
    port: int,
    timeout: float,
) -> tuple[ResolvedAddress, ...]:
    """Resolve once, rejecting the complete result if any answer is non-public."""
    if port not in ALLOWED_DESTINATION_PORTS:
        raise ProxyRejection(403, "port_denied")
    normalized, literal = _normalize_host(host)

    if literal is not None:
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return (ResolvedAddress(family, literal.compressed, port),)

    try:
        records = await asyncio.wait_for(_getaddrinfo(normalized, port), timeout)
    except TimeoutError as exc:
        raise ProxyRejection(504, "dns_timeout") from exc
    except (socket.gaierror, OSError) as exc:
        raise ProxyRejection(502, "dns_failure") from exc

    addresses: list[ResolvedAddress] = []
    seen: set[tuple[int, str]] = set()
    for family, socktype, _proto, _canonname, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        if socktype not in {0, socket.SOCK_STREAM}:
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (ValueError, TypeError, IndexError) as exc:
            raise ProxyRejection(403, "destination_denied") from exc
        if not _public_ip(address):
            # Do not select only the convenient public answer.  Rejecting a
            # mixed answer set prevents DNS rebinding and resolver-order games.
            raise ProxyRejection(403, "destination_denied")
        key = (family, address.compressed)
        if key not in seen:
            seen.add(key)
            addresses.append(ResolvedAddress(family, address.compressed, port))

    if not addresses:
        raise ProxyRejection(502, "dns_failure")
    return tuple(addresses)


async def _connect_ip(
    address: ResolvedAddress,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect a non-blocking socket to a numeric IP without another DNS query."""
    sock = socket.socket(address.family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setblocking(False)
    destination: tuple
    if address.family == socket.AF_INET6:
        destination = (address.ip, address.port, 0, 0)
    else:
        destination = (address.ip, address.port)
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.sock_connect(sock, destination), timeout)
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise


async def open_pinned_destination(
    addresses: Sequence[ResolvedAddress],
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Try vetted IPs within one bounded connect window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for address in addresses:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            return await _connect_ip(address, remaining)
        except (OSError, TimeoutError):
            continue
    raise ProxyRejection(502, "upstream_connect_failed")


def _parse_header_block(block: bytes, config: ProxyConfig) -> ProxyRequest:
    if len(block) > config.max_header_bytes:
        raise ProxyRejection(431, "headers_too_large")
    if not block.endswith(b"\r\n\r\n"):
        raise ProxyRejection(400, "malformed_request")

    lines = block[:-4].split(b"\r\n")
    if not lines or len(lines[0]) > config.max_header_line_bytes:
        raise ProxyRejection(400, "malformed_request")
    parts = lines[0].split(b" ")
    if len(parts) != 3 or not _HTTP_TOKEN.fullmatch(parts[0]):
        raise ProxyRejection(400, "malformed_request")
    try:
        method = parts[0].decode("ascii").upper()
        target = parts[1].decode("ascii")
        version = parts[2].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProxyRejection(400, "malformed_request") from exc
    if (
        version not in {"HTTP/1.0", "HTTP/1.1"}
        or not target
        or any(ord(char) <= 32 or ord(char) == 127 for char in target)
    ):
        raise ProxyRejection(400, "malformed_request")

    if len(lines) - 1 > config.max_header_count:
        raise ProxyRejection(431, "headers_too_large")
    headers: list[Header] = []
    for line in lines[1:]:
        if (
            not line
            or len(line) > config.max_header_line_bytes
            or line[:1] in {b" ", b"\t"}
            or b":" not in line
        ):
            raise ProxyRejection(400, "malformed_request")
        name, value = line.split(b":", 1)
        value = value.strip(b" \t")
        if (
            not _HTTP_TOKEN.fullmatch(name)
            or any(byte < 32 and byte != 9 for byte in value)
            or 127 in value
        ):
            raise ProxyRejection(400, "malformed_request")
        headers.append(Header(name.lower(), value))

    _validate_framing(headers, method)
    return ProxyRequest(method, target, version, tuple(headers))


def _validate_framing(headers: Sequence[Header], method: str) -> None:
    content_lengths = [h.value for h in headers if h.name == b"content-length"]
    transfer_encodings = [h.value for h in headers if h.name == b"transfer-encoding"]
    hosts = [h.value for h in headers if h.name == b"host"]
    if len(hosts) > 1 or len(content_lengths) > 1 or len(transfer_encodings) > 1:
        raise ProxyRejection(400, "ambiguous_headers")
    if content_lengths and transfer_encodings:
        raise ProxyRejection(400, "ambiguous_headers")
    connection_tokens = {
        token.strip().lower()
        for header in headers
        if header.name == b"connection"
        for token in header.value.split(b",")
        if token.strip()
    }
    if connection_tokens & {b"content-length", b"host", b"transfer-encoding"}:
        # Removing one of these fields while streaming the unchanged body could
        # create a different message boundary at the upstream server.
        raise ProxyRejection(400, "ambiguous_headers")
    if content_lengths:
        try:
            length = int(content_lengths[0])
        except ValueError as exc:
            raise ProxyRejection(400, "malformed_request") from exc
        if length < 0 or str(length).encode() != content_lengths[0]:
            raise ProxyRejection(400, "malformed_request")
    if transfer_encodings and transfer_encodings[0].lower() != b"chunked":
        raise ProxyRejection(400, "unsupported_transfer_encoding")
    if method == "CONNECT" and (
        transfer_encodings or (content_lengths and int(content_lengths[0]) != 0)
    ):
        raise ProxyRejection(400, "connect_body_denied")


def _parse_authority(authority: str) -> tuple[str, int]:
    if any(char in authority for char in "/?#"):
        raise ProxyRejection(400, "malformed_target")
    parsed = urlsplit(f"//{authority}")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ProxyRejection(400, "malformed_target")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyRejection(400, "malformed_target") from exc
    if port is None:
        raise ProxyRejection(400, "missing_port")
    host, _literal = _normalize_host(parsed.hostname)
    if port not in ALLOWED_DESTINATION_PORTS:
        raise ProxyRejection(403, "port_denied")
    return host, port


def _request_destination(request: ProxyRequest) -> Destination:
    if request.method == "CONNECT":
        host, port = _parse_authority(request.target)
        return Destination(host, port, None)

    try:
        parsed = urlsplit(request.target)
    except ValueError as exc:
        raise ProxyRejection(400, "malformed_target") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProxyRejection(400, "absolute_http_url_required")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ProxyRejection(400, "malformed_target") from exc
    if port not in ALLOWED_DESTINATION_PORTS:
        raise ProxyRejection(403, "port_denied")
    host, _literal = _normalize_host(parsed.hostname)
    path = parsed.path or "/"
    origin_target = urlunsplit(SplitResult("", "", path, parsed.query, ""))
    return Destination(host, port, origin_target)


def _host_header(host: str, port: int) -> bytes:
    display = f"[{host}]" if ":" in host else host
    if port == 80:
        return display.encode("ascii")
    return f"{display}:{port}".encode("ascii")


def _forward_request_head(request: ProxyRequest, destination: Destination) -> bytes:
    connection_tokens: set[bytes] = set()
    for header in request.headers:
        if header.name == b"connection":
            connection_tokens.update(
                token.strip().lower()
                for token in header.value.split(b",")
                if token.strip()
            )
    stripped = {
        b"connection",
        b"host",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"upgrade",
    } | connection_tokens

    lines = [
        f"{request.method} {destination.origin_target} {request.version}".encode(
            "ascii"
        ),
        b"Host: " + _host_header(destination.host, destination.port),
    ]
    for header in request.headers:
        if header.name not in stripped:
            lines.append(header.name + b": " + header.value)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


async def _read_request_head(
    reader: asyncio.StreamReader,
    config: ProxyConfig,
) -> ProxyRequest:
    try:
        block = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            config.header_timeout_seconds,
        )
    except asyncio.LimitOverrunError as exc:
        raise ProxyRejection(431, "headers_too_large") from exc
    except asyncio.IncompleteReadError as exc:
        raise ProxyRejection(400, "incomplete_headers") from exc
    except TimeoutError as exc:
        raise ProxyRejection(408, "header_timeout") from exc
    return _parse_header_block(block, config)


async def _drain(writer: asyncio.StreamWriter, timeout: float) -> None:
    await asyncio.wait_for(writer.drain(), timeout)


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    drain_timeout: float,
) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await _drain(writer, drain_timeout)
    try:
        writer.write_eof()
        await _drain(writer, drain_timeout)
    except (AttributeError, NotImplementedError, OSError, RuntimeError):
        pass


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    drain_timeout: float,
) -> None:
    client_to_upstream = asyncio.create_task(
        _copy_stream(client_reader, upstream_writer, drain_timeout)
    )
    upstream_to_client = asyncio.create_task(
        _copy_stream(upstream_reader, client_writer, drain_timeout)
    )
    tasks = {client_to_upstream, upstream_to_client}
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if upstream_to_client in done:
            # The HTTP response/tunnel peer is finished; the client commonly
            # keeps its write side open while waiting for this event.
            client_to_upstream.cancel()
        else:
            # A client half-close may be followed by the upstream response.
            await upstream_to_client
        for task in done:
            if not task.cancelled():
                task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def _send_error(writer: asyncio.StreamWriter, status: int) -> None:
    reason = _STATUS_REASONS.get(status, "Proxy Error")
    body = b"Proxy request denied.\n"
    response = (
        f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
        + b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    try:
        writer.write(response)
        await asyncio.wait_for(writer.drain(), 2.0)
    except (ConnectionError, OSError, TimeoutError):
        pass


async def _send_health(writer: asyncio.StreamWriter) -> None:
    body = b"ok\n"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Cache-Control: no-store\r\n"
        + b"Connection: close\r\n\r\n"
        + body
    )
    try:
        writer.write(response)
        await asyncio.wait_for(writer.drain(), 2.0)
    except (ConnectionError, OSError, TimeoutError):
        pass


def _peer_is_loopback(writer: asyncio.StreamWriter) -> bool:
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        return False
    try:
        address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return bool(
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped is not None
        and address.ipv4_mapped.is_loopback
    )


def _peer_label(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return "unknown"


class FilteringProxy:
    """Async HTTP proxy with destination admission and pinned connections."""

    def __init__(
        self,
        config: ProxyConfig | None = None,
        *,
        resolver: Resolver = resolve_public_destination,
        connector: Connector = open_pinned_destination,
    ):
        self.config = config or ProxyConfig()
        self._resolver = resolver
        self._connector = connector
        self._slots = asyncio.Semaphore(self.config.max_connections)

    async def start(self) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle_client,
            self.config.bind_host,
            self.config.bind_port,
            limit=self.config.max_header_bytes + 1,
        )

    async def handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        peer = _peer_label(client_writer)
        acquired = False
        upstream_writer: asyncio.StreamWriter | None = None
        committed = False
        try:
            try:
                await asyncio.wait_for(
                    self._slots.acquire(), self.config.header_timeout_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise ProxyRejection(503, "connection_limit") from exc

            request = await _read_request_head(client_reader, self.config)
            if request.method == "GET" and request.target == HEALTH_PATH:
                if not _peer_is_loopback(client_writer):
                    raise ProxyRejection(403, "health_peer_denied")
                await _send_health(client_writer)
                committed = True
                return

            destination = _request_destination(request)
            addresses = await self._resolver(
                destination.host,
                destination.port,
                self.config.dns_timeout_seconds,
            )
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._connector(addresses, self.config.connect_timeout_seconds),
                self.config.connect_timeout_seconds,
            )

            logger.info(
                "auth egress allowed method=%s destination=%s:%d",
                request.method,
                destination.host,
                destination.port,
            )
            if request.method == "CONNECT":
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await _drain(client_writer, self.config.drain_timeout_seconds)
            else:
                upstream_writer.write(_forward_request_head(request, destination))
                await _drain(upstream_writer, self.config.drain_timeout_seconds)
            committed = True

            async with asyncio.timeout(self.config.connection_timeout_seconds):
                await _relay(
                    client_reader,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    self.config.drain_timeout_seconds,
                )
        except ProxyRejection as exc:
            logger.warning(
                "auth egress denied code=%s peer=%s",
                exc.code,
                peer,
            )
            if not committed:
                await _send_error(client_writer, exc.status)
        except TimeoutError:
            logger.warning("auth egress denied code=timeout peer=%s", peer)
            if not committed:
                await _send_error(client_writer, 504)
        except (ConnectionError, OSError, ValueError):
            # Exception strings can contain a requested hostname or URL.  The
            # classification is useful; the raw exception is intentionally not.
            logger.warning("auth egress denied code=io_failure peer=%s", peer)
            if not committed:
                await _send_error(client_writer, 502)
        except Exception:
            # Do not attach ``exc_info``: arbitrary exception messages may
            # include the requested URL or a credential-bearing header.
            logger.error("auth egress internal failure peer=%s", peer)
            if not committed:
                await _send_error(client_writer, 502)
        finally:
            await _close_writer(upstream_writer)
            await _close_writer(client_writer)
            if acquired:
                self._slots.release()


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def config_from_env() -> ProxyConfig:
    return ProxyConfig(
        bind_host=os.environ.get("AUTH_EGRESS_BIND_HOST", "0.0.0.0").strip()
        or "0.0.0.0",
        bind_port=_env_int(
            "AUTH_EGRESS_PORT", 8080, minimum=1, maximum=65_535
        ),
        max_header_bytes=_env_int(
            "AUTH_EGRESS_MAX_HEADER_BYTES",
            32 * 1024,
            minimum=1_024,
            maximum=1024 * 1024,
        ),
        header_timeout_seconds=float(
            os.environ.get("AUTH_EGRESS_HEADER_TIMEOUT_SECONDS", "10")
        ),
        dns_timeout_seconds=float(
            os.environ.get("AUTH_EGRESS_DNS_TIMEOUT_SECONDS", "10")
        ),
        connect_timeout_seconds=float(
            os.environ.get("AUTH_EGRESS_CONNECT_TIMEOUT_SECONDS", "10")
        ),
        connection_timeout_seconds=float(
            os.environ.get("AUTH_EGRESS_CONNECTION_TIMEOUT_SECONDS", "600")
        ),
        max_connections=_env_int(
            "AUTH_EGRESS_MAX_CONNECTIONS", 64, minimum=1, maximum=10_000
        ),
    )


async def _serve(config: ProxyConfig) -> None:
    proxy = FilteringProxy(config)
    server = await proxy.start()
    logger.info(
        "auth egress proxy listening host=%s port=%d",
        config.bind_host,
        config.bind_port,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_serve(config_from_env()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
