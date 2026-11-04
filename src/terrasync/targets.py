"""Validating where an organisation administrator may point alert deliveries.

A webhook URL is an instruction to this server to make an HTTP request. Left
unchecked, any org admin could aim it at the cloud metadata endpoint or a
service on the private network the server runs in. So targets must be HTTPS
and must resolve only to public addresses, checked when the destination is
created and again before each send (a name can be re-pointed after creation).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urlparse

Resolver = Callable[[str], list[str]]

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TargetError(ValueError):
    pass


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise TargetError(f"{host} does not resolve: {e}") from e
    return sorted({i[4][0] for i in infos})


def _private(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    return not ip.is_global


def check_webhook(url: str, *, allow_private: bool = False,
                  resolver: Resolver = system_resolver) -> str:
    """Returns the URL if acceptable, else raises TargetError with the reason."""
    u = urlparse(url.strip())
    if u.scheme != "https":
        if not (allow_private and u.scheme == "http"):
            raise TargetError("webhook URL must use https")
    if not u.hostname:
        raise TargetError("webhook URL has no host")
    if u.username or u.password:
        raise TargetError("put credentials in the signing secret, not the URL")
    if allow_private:
        return url.strip()
    try:
        addresses = [u.hostname] if ipaddress.ip_address(u.hostname) else []
    except ValueError:
        addresses = resolver(u.hostname)
    if not addresses:
        raise TargetError(f"{u.hostname} does not resolve")
    bad = [a for a in addresses if _private(a)]
    if bad:
        raise TargetError(
            f"{u.hostname} resolves to a non-public address ({', '.join(bad)}); "
            "set TERRASYNC_WEBHOOK_ALLOW_PRIVATE=1 for on-premise receivers")
    return url.strip()


def check_email(address: str) -> str:
    a = address.strip()
    if not EMAIL.match(a) or len(a) > 254:
        raise TargetError("not an email address")
    return a
