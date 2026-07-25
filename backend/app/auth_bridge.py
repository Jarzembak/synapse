"""Fixed-purpose TCP bridge between the API and isolated browser network.

The browser network contains no Synapse application services.  This bridge
exposes only Selenium WebDriver and noVNC back to the ordinary Compose network;
it is not a general forward proxy.
"""
from __future__ import annotations

import asyncio
import logging


log = logging.getLogger("synapse.auth_bridge")
_TARGET = "auth-browser"
_PORTS = (4444, 7900)


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_port: int,
) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(_TARGET, target_port),
            timeout=10,
        )
    except (OSError, TimeoutError):
        client_writer.close()
        await client_writer.wait_closed()
        return
    tasks = {
        asyncio.create_task(_copy(client_reader, upstream_writer)),
        asyncio.create_task(_copy(upstream_reader, client_writer)),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def main() -> None:
    servers = []
    for port in _PORTS:
        server = await asyncio.start_server(
            lambda reader, writer, target_port=port: _relay(
                reader, writer, target_port
            ),
            host="0.0.0.0",
            port=port,
            limit=128 * 1024,
        )
        servers.append(server)
    log.info("authentication bridge ready")
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
