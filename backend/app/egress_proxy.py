"""SSRF-blocking forward proxy: the enforcement point of the worker egress boundary.

Workers (yt-dlp, aria2c, ffmpeg) are configured with this proxy *and* run on a Docker
network with no route to the internet, so a tool that ignores the proxy simply cannot
connect. The proxy resolves DNS itself, requires every resolved address to be a public
unicast address, then connects to the *validated IP* (never re-resolving), which defeats
DNS rebinding. Redirects, manifests and fragments all pass through it because the clients
issue new requests via the proxy. Only ports in ``allowed_ports`` are reachable.

Supported: ``CONNECT host:port`` (HTTPS) and absolute-URI ``GET/HEAD/...`` (plain HTTP,
one request per connection). Anything else is refused.

Run: ``python -m app.egress_proxy`` (listens on ``YTARIA_PROXY_BIND``, default 0.0.0.0:3128).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from urllib.parse import urlsplit

from .config import get_settings
from .logging_utils import configure_logging
from .services.urlpolicy import DestinationBlocked, resolve_public

log = logging.getLogger("ytaria.egress")

MAX_HEAD_BYTES = 16 * 1024
HEAD_TIMEOUT = 15.0
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 120.0
MAX_CONNECTION_SECONDS = 4 * 3600
MAX_CONNECTIONS = 256

_FORBIDDEN = (
    b"HTTP/1.1 403 Forbidden\r\nX-Ytaria-Egress: blocked\r\nContent-Type: text/plain\r\n"
    b"Connection: close\r\nContent-Length: 27\r\n\r\ndestination blocked by policy"
)
_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"


class EgressProxy:
    def __init__(self, allowed_ports: list[int]) -> None:
        self.allowed_ports = allowed_ports
        self._slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def _resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: resolve_public(host, port, allowed_ports=self.allowed_ports))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                await asyncio.wait_for(self._serve(reader, writer), timeout=MAX_CONNECTION_SECONDS)
            except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                pass
            except Exception:  # pragma: no cover - defensive
                log.exception("proxy error")
            finally:
                try:
                    writer.close()
                except Exception:  # pragma: no cover
                    pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT)
        except (asyncio.LimitOverrunError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            writer.write(_BAD_REQUEST)
            await writer.drain()
            return
        if len(head) > MAX_HEAD_BYTES:
            writer.write(_BAD_REQUEST)
            await writer.drain()
            return
        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            writer.write(_BAD_REQUEST)
            await writer.drain()
            return

        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]")
            try:
                port = int(port_s)
            except ValueError:
                writer.write(_BAD_REQUEST)
                await writer.drain()
                return
            upstream = await self._open(host, port, writer)
            if upstream is None:
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pipe(reader, writer, *upstream)
            return

        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            writer.write(_BAD_REQUEST)
            await writer.drain()
            return
        port = parts.port or 80
        upstream = await self._open(parts.hostname, port, writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        out = [f"{method} {path} HTTP/1.1".encode("latin-1")]
        for line in lines[1:]:
            if not line:
                continue
            name = line.split(b":", 1)[0].strip().lower()
            if name in (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"):
                continue
            out.append(line)
        out.append(b"Connection: close")
        up_writer.write(b"\r\n".join(out) + b"\r\n\r\n")
        await up_writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _open(self, host: str, port: int, client_writer: asyncio.StreamWriter):
        try:
            addresses = await self._resolve(host, port)
        except DestinationBlocked as exc:
            log.warning("blocked host=%s port=%s reason=%s", host, port, exc)
            client_writer.write(_FORBIDDEN)
            await client_writer.drain()
            return None
        last_error: Exception | None = None
        for address in addresses:  # connect to the validated IP, never re-resolve the name
            try:
                conn = await asyncio.wait_for(asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT)
                log.info("allowed host=%s port=%s", host, port)
                return conn
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
        log.info("connect failed host=%s port=%s err=%s", host, port, type(last_error).__name__)
        client_writer.write(_BAD_GATEWAY)
        await client_writer.drain()
        return None

    @staticmethod
    async def _pipe(
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await asyncio.wait_for(src.read(65536), timeout=IDLE_TIMEOUT)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # pragma: no cover
                    pass

        tasks = [asyncio.create_task(pump(c_reader, u_writer)), asyncio.create_task(pump(u_reader, c_writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            u_writer.close()


async def serve(host: str, port: int, allowed_ports: list[int]) -> None:
    proxy = EgressProxy(allowed_ports)
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEAD_BYTES)
    log.info("egress proxy listening on %s:%s (ports %s)", host, port, allowed_ports)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()


def main() -> None:
    configure_logging()
    settings = get_settings()
    bind = os.environ.get("YTARIA_PROXY_BIND", "0.0.0.0:3128")
    host, _, port = bind.rpartition(":")
    asyncio.run(serve(host, int(port), settings.allowed_ports))


if __name__ == "__main__":
    main()
