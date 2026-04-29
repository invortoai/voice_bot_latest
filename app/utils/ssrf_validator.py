"""
SSRF validation for webhook callback URLs.

Rejects private IPs, non-HTTPS schemes, AWS metadata endpoints,
and link-local addresses before any outbound HTTP request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from loguru import logger


class SSRFError(Exception):
    """Raised when a URL fails SSRF validation."""


async def validate_callback_url(url: str) -> None:
    """Validate that a callback URL is safe for outbound delivery.

    Raises SSRFError if the URL targets a private network, uses non-HTTPS,
    or resolves to a restricted IP address.
    """
    parsed = urlparse(url)

    # Scheme must be HTTPS
    if parsed.scheme != "https":
        raise SSRFError(f"Webhook URL must use HTTPS, got: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("Webhook URL has no hostname")

    # Reject obvious localhost
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise SSRFError(f"Webhook URL targets localhost: {hostname}")

    # Resolve hostname to IP and validate (run blocking DNS in a thread)
    try:
        loop = asyncio.get_running_loop()
        addr_infos = await loop.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise SSRFError(f"Cannot resolve webhook hostname '{hostname}': {exc}") from exc

    for addr_info in addr_infos:
        ip_str = addr_info[4][0]
        ip = ipaddress.ip_address(ip_str)

        if ip.is_private:
            raise SSRFError(f"Webhook URL resolves to private IP: {ip_str}")

        if ip.is_loopback:
            raise SSRFError(f"Webhook URL resolves to loopback: {ip_str}")

        if ip.is_link_local:
            raise SSRFError(f"Webhook URL resolves to link-local: {ip_str}")

        if ip.is_reserved:
            raise SSRFError(f"Webhook URL resolves to reserved IP: {ip_str}")

        if ip.is_multicast:
            raise SSRFError(f"Webhook URL resolves to multicast address: {ip_str}")

        if ip.is_unspecified:
            raise SSRFError(f"Webhook URL resolves to unspecified address: {ip_str}")

        # Block IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            mapped = ip.ipv4_mapped
            if (
                mapped.is_private
                or mapped.is_loopback
                or mapped.is_reserved
                or mapped.is_link_local
            ):
                raise SSRFError(
                    f"Webhook URL resolves to IPv4-mapped private address: {ip_str}"
                )

        # Block AWS metadata endpoint
        if ip_str in ("169.254.169.254", "::ffff:169.254.169.254"):
            raise SSRFError("Webhook URL resolves to AWS metadata endpoint")

    logger.debug(f"SSRF validation passed for {url} (hostname={hostname})")
