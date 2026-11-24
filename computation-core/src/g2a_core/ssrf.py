from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .schemas import G2AError


def validate_outbound_url_against_ssrf(url: str, allow_loopback: bool = False) -> None:
    """Enforces Section 7.1 outbound transport & SSRF rules prior to connection."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise G2AError("invalid_arguments", 400, "Credentials MUST NOT be embedded in endpoint URLs")

    scheme = (parsed.scheme or "").lower()
    if allow_loopback:
        if scheme not in ("https", "http"):
            raise G2AError("invalid_arguments", 400, f"Unsupported URL scheme {scheme!r}")
    else:
        if scheme != "https":
            raise G2AError("invalid_arguments", 400, "Remote Hub endpoints MUST use https:// (TLS 1.3)")

    host = parsed.hostname
    if not host:
        raise G2AError("invalid_arguments", 400, "Endpoint URL is missing a valid hostname")

    port = parsed.port or (443 if scheme == "https" else 80)
    if port <= 0 or port > 65535:
        raise G2AError("invalid_arguments", 400, "Invalid endpoint TCP port")

    # Resolve IP literals or DNS records to guard against SSRF and DNS rebinding to private/link-local space
    resolved_ips = []
    try:
        resolved_ips.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            for info in infos:
                resolved_ips.append(ipaddress.ip_address(info[4][0]))
        except socket.gaierror as exc:
            raise G2AError("unavailable", 503, f"Unable to resolve Hub host {host!r}: {exc}") from exc

    for raw_ip in resolved_ips:
        ip = raw_ip.ipv4_mapped if isinstance(raw_ip, ipaddress.IPv6Address) and raw_ip.ipv4_mapped is not None else raw_ip
        if ip.is_loopback:
            if not allow_loopback:
                raise G2AError("permission_denied", 403, "Loopback Hub address blocked by remote SSRF policy")
            continue
        if (
            ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            raise G2AError("permission_denied", 403, f"Private or link-local IP {ip} blocked by SSRF guard")
        if scheme != "https":
            raise G2AError("invalid_arguments", 400, "Non-loopback Hub connections MUST use https://")
