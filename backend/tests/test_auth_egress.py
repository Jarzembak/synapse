from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

import pytest

from app import auth_egress


PUBLIC_V4 = "93.184.216.34"


def _run(awaitable):
    return asyncio.run(awaitable)


def _record(
    ip: str,
    port: int = 443,
    family: socket.AddressFamily = socket.AF_INET,
) -> tuple:
    sockaddr = (ip, port) if family == socket.AF_INET else (ip, port, 0, 0)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "api",
        "redis",
        "metadata.google.internal",
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "168.63.129.16",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
    ],
)
def test_private_special_and_single_label_destinations_are_rejected(host: str):
    with pytest.raises(auth_egress.ProxyRejection) as exc:
        _run(auth_egress.resolve_public_destination(host, 443, 0.2))
    assert exc.value.status == 403


def test_mixed_dns_answer_is_rejected_instead_of_selecting_public(
    monkeypatch: pytest.MonkeyPatch,
):
    async def mixed_answer(_host: str, port: int):
        return [
            _record(PUBLIC_V4, port),
            _record("10.0.0.7", port),
        ]

    monkeypatch.setattr(auth_egress, "_getaddrinfo", mixed_answer)

    with pytest.raises(auth_egress.ProxyRejection) as exc:
        _run(
            auth_egress.resolve_public_destination(
                "public.example.com", 443, 0.2
            )
        )
    assert exc.value.code == "destination_denied"


def test_public_dns_answers_are_deduplicated_and_port_is_restricted(
    monkeypatch: pytest.MonkeyPatch,
):
    async def public_answer(_host: str, port: int):
        return [
            _record(PUBLIC_V4, port),
            _record(PUBLIC_V4, port),
            _record("2606:4700:4700::1111", port, socket.AF_INET6),
        ]

    monkeypatch.setattr(auth_egress, "_getaddrinfo", public_answer)
    addresses = _run(
        auth_egress.resolve_public_destination("public.example.com.", 443, 0.2)
    )
    assert addresses == (
        auth_egress.ResolvedAddress(socket.AF_INET, PUBLIC_V4, 443),
        auth_egress.ResolvedAddress(
            socket.AF_INET6, "2606:4700:4700::1111", 443
        ),
    )

    with pytest.raises(auth_egress.ProxyRejection) as exc:
        _run(
            auth_egress.resolve_public_destination(
                "public.example.com", 4444, 0.2
            )
        )
    assert exc.value.code == "port_denied"


def test_pinned_connector_receives_numeric_answer_without_reresolving(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[tuple[str, int]] = []
    sentinel_reader = object()
    sentinel_writer = object()

    async def connect_ip(address, _timeout):
        seen.append((address.ip, address.port))
        return sentinel_reader, sentinel_writer

    monkeypatch.setattr(auth_egress, "_connect_ip", connect_ip)
    result = _run(
        auth_egress.open_pinned_destination(
            (
                auth_egress.ResolvedAddress(
                    socket.AF_INET, PUBLIC_V4, 443
                ),
            ),
            0.2,
        )
    )
    assert result == (sentinel_reader, sentinel_writer)
    assert seen == [(PUBLIC_V4, 443)]


async def _start_server(
    handler: Callable[
        [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
    ],
    *,
    limit: int = 64 * 1024,
) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(
        handler, "127.0.0.1", 0, limit=limit
    )
    return server, server.sockets[0].getsockname()[1]


def test_local_health_request_succeeds_without_proxy_resolution(caplog):
    async def scenario():
        resolver_called = False

        async def resolver(_host, _port, _timeout):
            nonlocal resolver_called
            resolver_called = True
            return ()

        proxy = auth_egress.FilteringProxy(
            auth_egress.ProxyConfig(bind_host="127.0.0.1", bind_port=0),
            resolver=resolver,
        )
        server = await proxy.start()
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"GET {auth_egress.HEALTH_PATH} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1.0)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        assert response.endswith(b"\r\n\r\nok\n")
        assert resolver_called is False

    caplog.set_level(logging.WARNING, logger=auth_egress.__name__)
    _run(scenario())
    assert "incomplete_headers" not in caplog.text


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        (("127.0.0.1", 50000), True),
        (("::1", 50000, 0, 0), True),
        (("::ffff:127.0.0.1", 50000, 0, 0), True),
        (("172.20.0.4", 50000), False),
        (("203.0.113.5", 50000), False),
        (None, False),
    ],
)
def test_health_peer_classifier_accepts_only_loopback(peer, expected):
    class PeerWriter:
        def get_extra_info(self, name):
            assert name == "peername"
            return peer

    assert auth_egress._peer_is_loopback(PeerWriter()) is expected


def test_health_request_from_nonloopback_peer_is_denied(
    monkeypatch: pytest.MonkeyPatch,
):
    async def scenario():
        resolver_called = False

        async def resolver(_host, _port, _timeout):
            nonlocal resolver_called
            resolver_called = True
            return ()

        monkeypatch.setattr(
            auth_egress,
            "_peer_is_loopback",
            lambda _writer: False,
        )
        proxy = auth_egress.FilteringProxy(
            auth_egress.ProxyConfig(bind_host="127.0.0.1", bind_port=0),
            resolver=resolver,
        )
        server = await proxy.start()
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"GET {auth_egress.HEALTH_PATH} HTTP/1.1\r\n"
                "Host: auth-egress\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1.0)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

        assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n")
        assert resolver_called is False

    _run(scenario())


def test_plain_http_is_filtered_rewritten_and_logs_are_redacted(caplog):
    async def scenario():
        captured: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        async def upstream_handler(reader, writer):
            try:
                captured.set_result(await reader.readuntil(b"\r\n\r\n"))
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                    b"Connection: close\r\n\r\nOK"
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream, upstream_port = await _start_server(upstream_handler)
        resolved_calls: list[tuple[str, int]] = []
        connected: list[tuple[auth_egress.ResolvedAddress, ...]] = []

        async def resolver(host, port, _timeout):
            resolved_calls.append((host, port))
            return (
                auth_egress.ResolvedAddress(socket.AF_INET, PUBLIC_V4, port),
            )

        async def connector(addresses, _timeout):
            connected.append(tuple(addresses))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        config = auth_egress.ProxyConfig(bind_host="127.0.0.1", bind_port=0)
        proxy = auth_egress.FilteringProxy(
            config, resolver=resolver, connector=connector
        )
        server = await proxy.start()
        proxy_port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            writer.write(
                b"GET http://public.example.com/watch?token=top-secret HTTP/1.1\r\n"
                b"Host: evil.internal\r\n"
                b"Proxy-Authorization: Basic private-credential\r\n"
                b"Cookie: session=needed-upstream\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1.0)
            writer.close()
            await writer.wait_closed()
            forwarded = await asyncio.wait_for(captured, 1.0)
        finally:
            server.close()
            upstream.close()
            await server.wait_closed()
            await upstream.wait_closed()

        assert b"HTTP/1.1 200 OK" in response
        assert (
            forwarded.split(b"\r\n", 1)[0]
            == b"GET /watch?token=top-secret HTTP/1.1"
        )
        assert b"\r\nhost: public.example.com\r\n" in forwarded.lower()
        assert b"Proxy-Authorization" not in forwarded
        assert b"evil.internal" not in forwarded
        assert b"cookie: session=needed-upstream" in forwarded
        assert resolved_calls == [("public.example.com", 80)]
        assert connected[0][0].ip == PUBLIC_V4

    caplog.set_level(logging.INFO, logger=auth_egress.__name__)
    _run(scenario())
    log_text = caplog.text
    assert "public.example.com:80" in log_text
    assert "top-secret" not in log_text
    assert "/watch" not in log_text
    assert "private-credential" not in log_text
    assert "needed-upstream" not in log_text


def test_connect_tunnel_relays_bytes_to_the_pinned_destination():
    async def scenario():
        async def echo_handler(reader, writer):
            try:
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream, upstream_port = await _start_server(echo_handler)
        resolver_count = 0
        connected: list[str] = []

        async def resolver(host, port, _timeout):
            nonlocal resolver_count
            resolver_count += 1
            assert (host, port) == ("secure.example.com", 443)
            return (
                auth_egress.ResolvedAddress(socket.AF_INET, PUBLIC_V4, port),
            )

        async def connector(addresses, _timeout):
            connected.append(addresses[0].ip)
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = auth_egress.FilteringProxy(
            auth_egress.ProxyConfig(bind_host="127.0.0.1", bind_port=0),
            resolver=resolver,
            connector=connector,
        )
        server = await proxy.start()
        proxy_port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            writer.write(
                b"CONNECT secure.example.com:443 HTTP/1.1\r\n"
                b"Host: secure.example.com:443\r\n\r\n"
            )
            await writer.drain()
            established = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), 1.0
            )
            assert established.startswith(
                b"HTTP/1.1 200 Connection Established"
            )
            writer.write(b"opaque TLS bytes")
            await writer.drain()
            echoed = await asyncio.wait_for(
                reader.readexactly(len(b"opaque TLS bytes")), 1.0
            )
            assert echoed == b"opaque TLS bytes"
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            upstream.close()
            await server.wait_closed()
            await upstream.wait_closed()

        assert resolver_count == 1
        assert connected == [PUBLIC_V4]

    _run(scenario())


def test_oversized_headers_are_rejected_before_resolution():
    async def scenario():
        resolver_called = False

        async def resolver(_host, _port, _timeout):
            nonlocal resolver_called
            resolver_called = True
            return ()

        config = auth_egress.ProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            max_header_bytes=1024,
            max_header_line_bytes=512,
        )
        proxy = auth_egress.FilteringProxy(config, resolver=resolver)
        server = await proxy.start()
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET http://public.example.com/ HTTP/1.1\r\nX-Large: "
                + b"x" * 1200
                + b"\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1.0)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

        assert response.startswith(b"HTTP/1.1 431 ")
        assert resolver_called is False

    _run(scenario())


def test_incomplete_headers_hit_the_bounded_header_timeout():
    async def scenario():
        config = auth_egress.ProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            header_timeout_seconds=0.05,
        )
        proxy = auth_egress.FilteringProxy(config)
        server = await proxy.start()
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET http://public.example.com/ HTTP/1.1\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1.0)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
        assert response.startswith(b"HTTP/1.1 408 ")

    _run(scenario())


@pytest.mark.parametrize(
    "raw_request",
    [
        b"CONNECT public.example.com:22 HTTP/1.1\r\n\r\n",
        b"GET http://public.example.com:8000/ HTTP/1.1\r\n\r\n",
        b"GET https://public.example.com/ HTTP/1.1\r\n\r\n",
        b"GET /origin-form HTTP/1.1\r\nHost: public.example.com\r\n\r\n",
        (
            b"POST http://public.example.com/ HTTP/1.1\r\n"
            b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n"
        ),
        (
            b"POST http://public.example.com/ HTTP/1.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: transfer-encoding\r\n\r\n"
        ),
        b"GET http://public.example.com/\tbad HTTP/1.1\r\n\r\n",
    ],
)
def test_unsafe_or_ambiguous_request_forms_are_rejected(raw_request: bytes):
    config = auth_egress.ProxyConfig()
    parsed = None
    try:
        parsed = auth_egress._parse_header_block(raw_request, config)
    except auth_egress.ProxyRejection:
        return
    with pytest.raises(auth_egress.ProxyRejection):
        auth_egress._request_destination(parsed)
