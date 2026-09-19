"""URL and destination policy.

Two layers:

* ``validate_source_url`` – syntactic policy applied by the API before anything is queued.
* ``check_public_host`` / ``is_public_ip`` – resolved-address policy used by the egress proxy
  (the enforcement point) and by the worker's pre-flight check when no proxy is configured.

Validating the initial URL is *not* sufficient: yt-dlp and aria2c follow redirects, fetch
manifests and fragments from other hosts. Only the egress boundary sees those requests.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
_CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f]")
_NUMERICISH_HOST = re.compile(r"^[0-9a-fx.]+$", re.IGNORECASE)


class UrlRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    host: str
    port: int
    url_hash: str


_EXTRA_BLOCKED = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",      # carrier-grade NAT
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
        "64:ff9b:1::/48",
        "100::/64",
        "2001:db8::/32",
    )
]


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    packed = ip.packed
    if packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):  # NAT64 well-known prefix
        return ipaddress.IPv4Address(packed[12:])
    return None


def is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses (IPv4 and IPv6)."""
    ip = ipaddress.ip_address(value) if isinstance(value, str) else value
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not is_public_ip(embedded):
            return False
        if ip.is_site_local:
            return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version)


def _host_matches(host: str, allowed: list[str]) -> bool:
    return any(host == a or host.endswith("." + a) for a in (h.lower().lstrip(".") for h in allowed))


def validate_source_url(
    raw: str,
    *,
    allowed_hosts: list[str],
    allowed_ports: list[int],
) -> ValidatedUrl:
    """Validate and normalise a user-submitted media URL. Raises ``UrlRejected``."""
    raw = (raw or "").strip()
    if not raw:
        raise UrlRejected("url_required", "Enter a URL.")
    if len(raw) > MAX_URL_LENGTH:
        raise UrlRejected("url_too_long", "That URL is too long.")
    if _CONTROL.search(raw):
        raise UrlRejected("url_invalid", "That URL contains invalid characters.")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UrlRejected("url_invalid", "That does not look like a valid URL.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlRejected("url_scheme", "Only http:// and https:// links are supported.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRejected("url_credentials", "Links containing a username or password are not accepted.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UrlRejected("url_invalid", "That link has no host name.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlRejected("url_invalid", "That link has an invalid host name.") from None

    literal: ipaddress._BaseAddress | None
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(literal):
            raise UrlRejected("url_destination", "Links to private or internal addresses are not allowed.")
        raise UrlRejected("url_host_not_supported", "Links must use a supported site's host name, not an IP address.")
    if _NUMERICISH_HOST.match(host):  # 2130706433, 0x7f.1, 017700000001 ...
        raise UrlRejected("url_destination", "That host name is not allowed.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise UrlRejected("url_destination", "Links to local or internal hosts are not allowed.")

    effective_port = port or (443 if parts.scheme.lower() == "https" else 80)
    if effective_port not in allowed_ports:
        raise UrlRejected("url_port", "That port is not allowed.")
    if not _host_matches(host, allowed_hosts):
        raise UrlRejected("url_host_not_supported", "That site is not supported yet.")

    netloc = host if port is None or port == {"http": 80, "https": 443}[parts.scheme.lower()] else f"{host}:{port}"
    normalized = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    return ValidatedUrl(
        url=normalized,
        host=host,
        port=effective_port,
        url_hash=hashlib.sha256(normalized.encode()).hexdigest(),
    )


class DestinationBlocked(Exception):
    pass


def resolve_public(host: str, port: int, *, allowed_ports: list[int] | None = None) -> list[str]:
    """Resolve ``host`` and require *every* address to be public. Returns the addresses.

    Callers that connect must connect to one of the returned addresses (not re-resolve) so a
    DNS-rebinding answer cannot change between check and use.
    """
    if allowed_ports is not None and port not in allowed_ports:
        raise DestinationBlocked(f"port {port} not allowed")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(literal):
            raise DestinationBlocked("destination address not allowed")
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DestinationBlocked("host could not be resolved") from exc
    addresses = []
    for family, _t, _p, _c, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        addr = sockaddr[0].split("%")[0]
        if not is_public_ip(addr):
            raise DestinationBlocked("destination address not allowed")
        addresses.append(addr)
    if not addresses:
        raise DestinationBlocked("host could not be resolved")
    return addresses
