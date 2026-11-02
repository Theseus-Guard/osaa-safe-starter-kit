"""The client address that the API uses for security decisions: demo sign-in and the login rate limits.

Rule. The socket peer is the only address the API takes on its own word. Forwarded headers are read only when the peer
is one of the SAFE_TRUSTED_PROXIES (config_guard.trusted_proxies). For a trusted peer the client is the rightmost
X-Forwarded-For entry that is not itself a trusted proxy. The walk starts at the right, because each proxy appends the
address it received the request from; entries further left were supplied by the client and are not used. If the walk
meets an entry that is not an IP address, the result is the trusted peer itself (fail closed). A forwarded header from
an untrusted peer is ignored, so a client cannot claim loopback by sending the header itself.

How the chain runs in this stack is described on the api service in compose.yaml and in gateway/nginx.conf.
"""

from collections.abc import Sequence
from ipaddress import IPv4Address, IPv6Address, ip_address

from safe_kit.config_guard import TrustedNetwork

UNKNOWN_ADDRESS = "unknown"


def _parse(text: str) -> IPv4Address | IPv6Address | None:
    """The address as an IPv4 or IPv6 object, or None when the text is not an IP address."""
    try:
        parsed = ip_address(text.strip())
    except ValueError:
        return None
    # An IPv4 client seen through a dual-stack socket arrives as ::ffff:a.b.c.d; compare and report it as IPv4.
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _is_trusted(address: IPv4Address | IPv6Address, trusted: Sequence[TrustedNetwork]) -> bool:
    return any(address in network for network in trusted)


def resolve_client_address(peer: str | None, forwarded_for: str | None, trusted: Sequence[TrustedNetwork]) -> str:
    """The address to use for a security decision. See the module docstring for the rule."""
    if not peer:
        return UNKNOWN_ADDRESS
    peer_ip = _parse(peer)
    if peer_ip is None or not forwarded_for or not _is_trusted(peer_ip, trusted):
        return str(peer_ip) if peer_ip is not None else peer
    for entry in reversed(forwarded_for.split(",")):
        hop = _parse(entry)
        if hop is None:
            return str(peer_ip)
        if not _is_trusted(hop, trusted):
            return str(hop)
    return str(peer_ip)
