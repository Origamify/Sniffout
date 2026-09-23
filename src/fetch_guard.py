"""Fetch guard: reject non-public http(s) targets before any network request."""

import ipaddress
import socket
from urllib.parse import urlsplit


def assert_public_http_url(url: str) -> None:
    """Raise ValueError unless url is http(s) and every resolved address is public.

    Blocks SSRF against loopback/private/link-local targets. DNS rebinding
    between this check and the actual fetch is out of scope.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host {host!r}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"non-public target address: {ip}")
