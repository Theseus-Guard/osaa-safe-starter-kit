"""Client address resolution (safe_kit.client_address): the one rule behind demo sign-in and the login rate limits.

These tests need no database. The API-level tests replace the rate limiter and the admin check with fakes.
"""

import pytest
from fastapi.testclient import TestClient

from safe_kit import api
from safe_kit.client_address import resolve_client_address
from safe_kit.config_guard import ConfigError, trusted_proxies

GATEWAY = "172.28.0.10"
TRUSTED = trusted_proxies({"SAFE_TRUSTED_PROXIES": GATEWAY})
CLIENT_NETWORK = trusted_proxies({"SAFE_TRUSTED_PROXIES": f"{GATEWAY},10.0.0.0/8"})


def test_without_a_trusted_proxy_the_socket_peer_is_used_and_the_header_is_ignored():
    assert resolve_client_address("203.0.113.9", "198.51.100.7", ()) == "203.0.113.9"
    assert resolve_client_address("203.0.113.9", None, ()) == "203.0.113.9"


def test_forged_loopback_from_an_untrusted_peer_is_not_trusted():
    # The most important case: a client sends the header itself and claims to be on loopback.
    assert resolve_client_address("203.0.113.9", "127.0.0.1", TRUSTED) == "203.0.113.9"
    assert resolve_client_address("203.0.113.9", "::1", TRUSTED) == "203.0.113.9"
    assert (
        resolve_client_address("203.0.113.9", "127.0.0.1, 10.1.2.3", CLIENT_NETWORK)
        == "203.0.113.9"
    )


def test_trusted_peer_with_one_forwarded_address_uses_that_address():
    assert resolve_client_address(GATEWAY, "198.51.100.7", TRUSTED) == "198.51.100.7"


def test_trusted_peer_with_a_chain_uses_the_rightmost_untrusted_entry():
    # The leftmost entry was written by the client and is ignored; 10.1.2.3 is a trusted proxy and is skipped.
    chain = "127.0.0.1, 198.51.100.7, 10.1.2.3"
    assert resolve_client_address(GATEWAY, chain, CLIENT_NETWORK) == "198.51.100.7"


def test_trusted_peer_with_only_trusted_entries_falls_back_to_the_peer():
    assert resolve_client_address(GATEWAY, "10.1.2.3", CLIENT_NETWORK) == GATEWAY


def test_trusted_peer_with_a_malformed_entry_fails_closed_to_the_peer():
    assert (
        resolve_client_address(GATEWAY, "198.51.100.7, not-an-address", TRUSTED)
        == GATEWAY
    )
    assert resolve_client_address(GATEWAY, "198.51.100.7,", TRUSTED) == GATEWAY


def test_trusted_peer_without_a_forwarded_header_uses_the_peer():
    assert resolve_client_address(GATEWAY, None, TRUSTED) == GATEWAY
    assert resolve_client_address(GATEWAY, "  ", TRUSTED) == GATEWAY


def test_ipv6_loopback_and_ipv4_mapped_peers_are_compared_as_addresses():
    assert resolve_client_address(GATEWAY, "::1", TRUSTED) == "::1"
    assert (
        resolve_client_address(f"::ffff:{GATEWAY}", "127.0.0.1", TRUSTED) == "127.0.0.1"
    )
    assert (
        resolve_client_address("::ffff:203.0.113.9", "127.0.0.1", TRUSTED)
        == "203.0.113.9"
    )


def test_missing_or_non_address_peer_is_reported_as_it_is_and_never_trusted():
    assert resolve_client_address(None, "127.0.0.1", TRUSTED) == "unknown"
    assert resolve_client_address("", "127.0.0.1", TRUSTED) == "unknown"
    assert resolve_client_address("testclient", "127.0.0.1", TRUSTED) == "testclient"


@pytest.mark.parametrize(
    "value",
    ["172.28.0.10/24", "not-an-address", "172.28.0.10,,10.0.0.1", "0.0.0.0/0", "::/0"],
)
def test_malformed_trusted_proxies_are_refused_at_startup(value):
    with pytest.raises(ConfigError, match="SAFE_TRUSTED_PROXIES"):
        trusted_proxies({"SAFE_TRUSTED_PROXIES": value})


def test_login_rate_limit_is_keyed_by_the_resolved_client_address(monkeypatch):
    keys = []

    def fake_hit(key, window):
        keys.append(key)
        return 10**6 if key.startswith("login:client:") else 1

    monkeypatch.setattr(api, "hit_rate_limit", fake_hit)
    monkeypatch.setattr(api, "admin_exists", lambda: True)
    monkeypatch.setenv("SAFE_TRUSTED_PROXIES", GATEWAY)
    body = {"username": "alice", "password": "wrong password here"}

    gateway = TestClient(api.app, client=(GATEWAY, 50000))
    assert (
        gateway.post(
            "/api/v1/auth/token", headers={"X-Forwarded-For": "203.0.113.9"}, json=body
        ).status_code
        == 429
    )
    # A rotating forwarded header from a client that is not a trusted proxy does not change the bucket.
    direct = TestClient(api.app, client=("198.51.100.20", 50000))
    for forged in ("10.9.9.9", "10.9.9.10"):
        response = direct.post(
            "/api/v1/auth/token",
            headers={"X-Forwarded-For": forged, "X-Real-IP": forged},
            json=body,
        )
        assert response.status_code == 429

    assert "login:client:203.0.113.9" in keys
    assert keys.count("login:client:198.51.100.20") == 2
    assert not any(key.endswith(("10.9.9.9", "10.9.9.10")) for key in keys)
