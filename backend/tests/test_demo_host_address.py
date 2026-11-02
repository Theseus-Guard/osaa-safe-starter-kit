"""The Docker host address counts as local for demo sign-in, in demo mode only (SAFE_DEMO_HOST_ADDRESS)."""

import pytest

from safe_kit.config_guard import ConfigError, demo_host_address
from safe_kit.demo_auth import demo_sign_in_allowed

# A complete, valid environment, so that demo_sign_in_allowed sees the same mode the startup guard sees.
DB_SETTINGS = {
    "SAFE_DB_HOST": "db.internal",
    "SAFE_DB_PORT": "5432",
    "SAFE_DB_NAME": "safe",
    "SAFE_DB_USER": "safe_api",
    "SAFE_DB_PASSWORD_FILE": "/run/secrets/db_app_password",
    "SAFE_ACCESS_TTL_MINUTES": "60",
    "SAFE_REFRESH_TTL_MINUTES": "60",
    "SAFE_SESSION_MAX_HOURS": "12",
}
DEMO = {"SAFE_MODE": "demo", "SAFE_BIND_HOST": "127.0.0.1", "SAFE_DATASET": "synthetic-demo", **DB_SETTINGS}
PRODUCTION = {"SAFE_MODE": "production", **DB_SETTINGS}
HOST = "172.28.0.1"


def test_host_address_is_accepted_in_demo_mode_when_configured():
    assert demo_sign_in_allowed({**DEMO, "SAFE_DEMO_HOST_ADDRESS": HOST}, HOST)


def test_host_address_is_refused_when_not_configured():
    assert not demo_sign_in_allowed(DEMO, HOST)


def test_other_addresses_on_the_edge_network_are_refused():
    env = {**DEMO, "SAFE_DEMO_HOST_ADDRESS": HOST}
    assert not demo_sign_in_allowed(env, "172.28.0.10")  # the gateway itself is not the client
    assert not demo_sign_in_allowed(env, "172.28.0.99")


def test_host_address_is_refused_in_production_mode():
    env = {**PRODUCTION, "SAFE_DEMO_HOST_ADDRESS": HOST}
    assert not demo_sign_in_allowed(env, HOST)


def test_loopback_still_works_in_demo_mode_without_a_host_address():
    assert demo_sign_in_allowed(DEMO, "127.0.0.1")
    assert demo_sign_in_allowed(DEMO, "::1")


def test_no_client_address_is_refused():
    assert not demo_sign_in_allowed({**DEMO, "SAFE_DEMO_HOST_ADDRESS": HOST}, None)


def test_empty_setting_means_no_host_address():
    assert demo_host_address({"SAFE_DEMO_HOST_ADDRESS": "  "}) is None
    assert demo_host_address({}) is None


@pytest.mark.parametrize("value", ["not-an-address", "172.28.0.1/24", "localhost"])
def test_malformed_host_address_is_refused_at_startup(value):
    with pytest.raises(ConfigError):
        demo_host_address({"SAFE_DEMO_HOST_ADDRESS": value})
