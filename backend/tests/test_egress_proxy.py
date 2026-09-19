"""The egress proxy is the SSRF enforcement point. These tests drive it over real sockets."""

import asyncio
import socket
import threading

import pytest

from app import egress_proxy
from app.services import urlpolicy


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, allowed_ports):
        self.port = _free_port()
        self.allowed_ports = allowed_ports
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        proxy = egress_proxy.EgressProxy(self.allowed_ports)

        async def main():
            self.server = await asyncio.start_server(proxy.handle, "127.0.0.1", self.port)
            self.ready.set()
            await self.server.serve_forever()

        self.main_task = self.loop.create_task(main())
        try:
            self.loop.run_until_complete(self.main_task)
        except asyncio.CancelledError:
            pass

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *a):
        self.loop.call_soon_threadsafe(self.main_task.cancel)
        self.thread.join(5)


def send(port, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        s.settimeout(3)
        data = b""
        try:
            while chunk := s.recv(4096):
                data += chunk
        except socket.timeout:
            pass
        return data


@pytest.mark.parametrize("target", ["127.0.0.1:80", "169.254.169.254:80", "10.0.0.1:443", "[::1]:443", "localhost:80", "192.168.1.1:80"])
def test_connect_to_internal_destinations_is_blocked(target):
    with Harness([80, 443]) as h:
        resp = send(h.port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.1 403") and b"X-Ytaria-Egress: blocked" in resp


def test_plain_http_to_metadata_and_loopback_blocked():
    with Harness([80, 443]) as h:
        for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:80/admin"):
            resp = send(h.port, f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            assert resp.startswith(b"HTTP/1.1 403"), resp


def test_disallowed_port_blocked_even_for_public_ip(monkeypatch):
    with Harness([443]) as h:
        resp = send(h.port, b"CONNECT 8.8.8.8:25 HTTP/1.1\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 403")


def test_dns_rebinding_answers_pinned_to_validated_ip(monkeypatch):
    """A name that resolves to a private address is refused; the proxy never re-resolves after validation."""
    calls = []

    real = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *a, **kw):
        if host != "rebind.example.com":
            return real(host, port, *a, **kw)
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with Harness([80]) as h:
        resp = send(h.port, b"CONNECT rebind.example.com:80 HTTP/1.1\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 403") and calls == ["rebind.example.com"]


def test_allowed_destination_is_tunnelled_and_forwarded(monkeypatch):
    """Positive path: with the policy relaxed for a local origin, both CONNECT and plain HTTP are relayed."""
    origin_port = _free_port()
    received = []

    def origin():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", origin_port))
        srv.listen(2)
        for _ in range(2):
            conn, _ = srv.accept()
            data = conn.recv(4096)
            received.append(data)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok" if data.startswith(b"GET") else b"pong")
            conn.close()
        srv.close()

    threading.Thread(target=origin, daemon=True).start()
    monkeypatch.setattr(egress_proxy, "resolve_public", lambda host, port, allowed_ports=None: ["127.0.0.1"])
    with Harness([origin_port]) as h:
        plain = send(h.port, f"GET http://example.test:{origin_port}/a?b=1 HTTP/1.1\r\nHost: example.test\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        assert plain.endswith(b"ok")
        with socket.create_connection(("127.0.0.1", h.port), timeout=5) as s:
            s.sendall(f"CONNECT example.test:{origin_port} HTTP/1.1\r\n\r\n".encode())
            assert s.recv(100).startswith(b"HTTP/1.1 200")
            s.sendall(b"ping")
            assert s.recv(100) == b"pong"
    assert received[0].startswith(b"GET /a?b=1 HTTP/1.1") and b"Proxy-Connection" not in received[0]


def test_malformed_requests_rejected():
    with Harness([80]) as h:
        assert send(h.port, b"GARBAGE\r\n\r\n").startswith(b"HTTP/1.1 400")
        assert send(h.port, b"GET / HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")  # origin-form, not a proxy request
        assert send(h.port, b"GET ftp://x/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")
