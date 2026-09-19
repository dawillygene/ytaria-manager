import pytest

from app.services.urlpolicy import DestinationBlocked, UrlRejected, is_public_ip, resolve_public, validate_source_url

ALLOWED = ["youtube.com", "youtu.be", "vimeo.com"]
PORTS = [80, 443]


def v(url):
    return validate_source_url(url, allowed_hosts=ALLOWED, allowed_ports=PORTS)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc",
    "http://youtu.be/abc",
    "https://YouTube.com:443/watch?v=abc#frag",
    "https://m.youtube.com/watch?v=abc",
])
def test_accepts_supported_urls(url):
    r = v(url)
    assert r.url.startswith(("http://", "https://")) and "#" not in r.url and len(r.url_hash) == 64


def test_normalises_default_port_and_host_case():
    assert v("https://YOUTUBE.com:443/x").url == "https://youtube.com/x"
    assert v("https://youtube.com/x").url_hash == v("https://YOUTUBE.COM:443/x#top").url_hash


@pytest.mark.parametrize("url,code", [
    ("", "url_required"),
    ("ftp://youtube.com/x", "url_scheme"),
    ("file:///etc/passwd", "url_scheme"),
    ("javascript:alert(1)", "url_scheme"),
    ("//youtube.com/x", "url_scheme"),
    ("https://user:pw@youtube.com/x", "url_credentials"),
    ("https://user@youtube.com/x", "url_credentials"),
    ("https://youtube.com@evil.com/x", "url_credentials"),
    ("https://localhost/x", "url_destination"),
    ("https://foo.localhost/x", "url_destination"),
    ("https://service.internal/x", "url_destination"),
    ("http://127.0.0.1/x", "url_destination"),
    ("http://[::1]/x", "url_destination"),
    ("http://169.254.169.254/latest/meta-data/", "url_destination"),
    ("http://10.0.0.5/", "url_destination"),
    ("http://192.168.1.1/", "url_destination"),
    ("http://2130706433/", "url_destination"),       # decimal 127.0.0.1
    ("http://0x7f.0.0.1/", "url_destination"),
    ("http://017700000001/", "url_destination"),
    ("http://8.8.8.8/", "url_host_not_supported"),   # public IP literal still not a supported site
    ("https://youtube.com:8080/x", "url_port"),
    ("https://youtube.com:22/x", "url_port"),
    ("https://evil.com/x", "url_host_not_supported"),
    ("https://youtube.com.evil.com/x", "url_host_not_supported"),
    ("https://notyoutube.com/x", "url_host_not_supported"),
    ("https://youtube.com/x\r\nHost: evil", "url_invalid"),
    ("https://youtube.com/ x", "url_invalid"),
    ("https://" + "a" * 3000 + ".com", "url_too_long"),
])
def test_rejects_unsafe_urls(url, code):
    with pytest.raises(UrlRejected) as exc:
        v(url)
    assert exc.value.code == code


def test_option_like_url_is_rejected():
    with pytest.raises(UrlRejected):
        v("--exec=touch /tmp/pwned")
    with pytest.raises(UrlRejected):
        v("-o /etc/passwd")


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("1.1.1.1", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("172.31.255.255", False), ("192.168.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False), ("255.255.255.255", False),
    ("198.18.0.1", False), ("::1", False), ("::", False), ("fe80::1", False), ("fc00::1", False), ("fd00:ec2::254", False),
    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False), ("::ffff:8.8.8.8", True),
    ("64:ff9b::7f00:1", False), ("2002:7f00:1::", False), ("ff02::1", False), ("2001:db8::1", False),
])
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


def test_resolve_public_blocks_loopback_and_bad_ports():
    for host in ("127.0.0.1", "::1", "10.0.0.1", "169.254.169.254"):
        with pytest.raises(DestinationBlocked):
            resolve_public(host, 443, allowed_ports=PORTS)
    with pytest.raises(DestinationBlocked):
        resolve_public("8.8.8.8", 22, allowed_ports=PORTS)
    assert resolve_public("8.8.8.8", 443, allowed_ports=PORTS) == ["8.8.8.8"]


def test_resolve_public_blocks_if_any_answer_is_private(monkeypatch):
    import socket

    def fake(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(DestinationBlocked):  # rebinding-style mixed answer
        resolve_public("rebind.example.com", 443)
