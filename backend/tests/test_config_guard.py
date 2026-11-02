"""Startup guard: the environment rules that decide whether the API or the worker may start (safe_kit.config_guard).

These tests pass explicit mappings to load_settings and never read the process environment, except where the API
lifespan and the worker entrypoint are exercised; those tests set or remove the variables themselves.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from safe_kit import api, db
from safe_kit.config_guard import ConfigError, load_settings

SECRET = "sentinel-secret-value-7f3a"
SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"

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


def _apply(base: dict, changes: dict) -> dict:
    env = dict(base)
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def demo_env(**changes) -> dict:
    base = {"SAFE_MODE": "demo", "SAFE_BIND_HOST": "127.0.0.1", "SAFE_DATASET": "synthetic-demo", **DB_SETTINGS}
    return _apply(base, changes)


def production_env(**changes) -> dict:
    return _apply({"SAFE_MODE": "production", **DB_SETTINGS}, changes)


def test_valid_demo_environment_is_accepted():
    settings = load_settings(demo_env())

    assert settings.mode == "demo"
    assert settings.bind_host == "127.0.0.1"
    assert settings.dataset == "synthetic-demo"
    assert settings.db_host == "db.internal"
    assert settings.db_port == 5432
    assert settings.db_name == "safe"
    assert settings.db_user == "safe_api"
    assert settings.db_password_file == "/run/secrets/db_app_password"


def test_valid_production_environment_is_accepted():
    settings = load_settings(production_env())

    assert settings.mode == "production"
    assert settings.bind_host is None
    assert settings.dataset is None


def test_production_accepts_an_empty_dataset_marker():
    assert load_settings(production_env(SAFE_DATASET="")).dataset is None
    assert load_settings(production_env(SAFE_DATASET="   ")).dataset is None


def test_production_does_not_restrict_the_bind_host():
    assert load_settings(production_env(SAFE_BIND_HOST="0.0.0.0")).bind_host == "0.0.0.0"


def test_production_refuses_the_synthetic_demo_marker():
    with pytest.raises(ConfigError, match="SAFE_DATASET"):
        load_settings(production_env(SAFE_DATASET="synthetic-demo"))


@pytest.mark.parametrize("dataset", ["real-cases", "synthetic-demo-2", " x "])
def test_production_refuses_any_other_dataset_marker(dataset):
    with pytest.raises(ConfigError, match="SAFE_DATASET"):
        load_settings(production_env(SAFE_DATASET=dataset))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_demo_accepts_loopback_published_hosts(host):
    assert load_settings(demo_env(SAFE_BIND_HOST=host)).bind_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "localhost", "LOCALHOST", "db", "example.com"])
def test_demo_refuses_a_published_host_that_is_not_loopback(host):
    with pytest.raises(ConfigError, match="published host address"):
        load_settings(demo_env(SAFE_BIND_HOST=host))


@pytest.mark.parametrize("name", ["SAFE_ACCESS_TTL_MINUTES", "SAFE_REFRESH_TTL_MINUTES", "SAFE_SESSION_MAX_HOURS"])
def test_each_token_setting_is_required(name):
    with pytest.raises(ConfigError, match=name):
        load_settings(production_env(**{name: None}))
    with pytest.raises(ConfigError, match=name):
        load_settings(production_env(**{name: "  "}))


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "sixty", str(525_601)])
def test_access_and_refresh_minutes_must_be_whole_numbers_in_range(value):
    with pytest.raises(ConfigError, match="SAFE_ACCESS_TTL_MINUTES"):
        load_settings(production_env(SAFE_ACCESS_TTL_MINUTES=value))
    with pytest.raises(ConfigError, match="SAFE_REFRESH_TTL_MINUTES"):
        load_settings(production_env(SAFE_REFRESH_TTL_MINUTES=value))


@pytest.mark.parametrize("value", ["0", "-12", "twelve", str(8_761)])
def test_session_hours_must_be_whole_numbers_in_range(value):
    with pytest.raises(ConfigError, match="SAFE_SESSION_MAX_HOURS"):
        load_settings(production_env(SAFE_SESSION_MAX_HOURS=value))


def test_token_settings_are_read_into_the_settings():
    settings = load_settings(production_env(SAFE_ACCESS_TTL_MINUTES="30", SAFE_REFRESH_TTL_MINUTES="90",
                                            SAFE_SESSION_MAX_HOURS="8"))
    assert (settings.access_ttl_minutes, settings.refresh_ttl_minutes, settings.session_max_hours) == (30, 90, 8)


@pytest.mark.parametrize("service", ["api", "worker"])
def test_compose_sets_the_token_settings_for_api_and_worker(service):
    # The guard runs in both processes, so both services carry the settings; the values are the ones that were the
    # defaults before, so the demo behaves as it did.
    compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
    lines = compose.split(f"\n  {service}:\n", 1)[1].split("\n")
    environment = []
    for line in lines[lines.index("    environment:") + 1:]:
        if line and not line.startswith("      "):
            break
        environment.append(line)
    text = "\n".join(environment)
    assert 'SAFE_ACCESS_TTL_MINUTES: "60"' in text
    assert 'SAFE_REFRESH_TTL_MINUTES: "60"' in text
    assert 'SAFE_SESSION_MAX_HOURS: "12"' in text


def test_compose_does_not_publish_the_api():
    # The gateway is the only entry point. The API is attached to backend, which is internal, so a published port
    # would not be reachable anyway; test_demo_egress.py checks the published ports of the whole file.
    compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
    api_block = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]

    assert "    ports:" not in api_block


def test_compose_demo_api_binds_the_published_address_to_loopback():
    compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
    api_block = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]

    assert "SAFE_BIND_HOST: 127.0.0.1" in api_block
    assert "SAFE_MODE: demo" in api_block


def test_compose_api_trusts_only_the_gateway_address_on_a_fixed_backend_subnet():
    # The API sees the gateway as its peer on backend (the gateway's edge address is only used by the host side).
    compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
    api_block = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    gateway_block = compose.split("\n  gateway:\n", 1)[1].split("\n  cli:\n", 1)[0]

    match = re.search(r'SAFE_TRUSTED_PROXIES: "([^"]*)"', api_block)
    assert match, "the api service must set SAFE_TRUSTED_PROXIES"
    trusted = match.group(1)
    assert trusted == "172.29.0.10"
    assert "ipv4_address: 172.29.0.10" in gateway_block
    assert "ipv4_address: 172.28.0.10" in gateway_block
    assert "subnet: 172.29.0.0/24" in compose
    assert "subnet: 172.28.0.0/24" in compose


def test_nginx_overwrites_x_forwarded_for_with_the_direct_peer():
    nginx = (Path(__file__).resolve().parents[2] / "gateway" / "nginx.conf").read_text(encoding="utf-8")

    assert nginx.count("proxy_set_header X-Forwarded-For $remote_addr;") == 2
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "listen 8443 ssl;" in nginx


def test_trusted_proxies_are_optional_and_allowed_in_production():
    assert load_settings(demo_env()).trusted_proxies == ()
    settings = load_settings(production_env(SAFE_TRUSTED_PROXIES="172.28.0.10, 10.0.0.0/8"))
    assert [str(network) for network in settings.trusted_proxies] == ["172.28.0.10/32", "10.0.0.0/8"]


@pytest.mark.parametrize("value", ["172.28.0.10/24", "172.28.0.10,,10.0.0.1", "not-an-address", "0.0.0.0/0"])
def test_malformed_trusted_proxies_stop_the_process_without_echoing_the_value(value):
    with pytest.raises(ConfigError, match="SAFE_TRUSTED_PROXIES") as refused:
        load_settings(demo_env(SAFE_TRUSTED_PROXIES=value + SECRET))

    assert SECRET not in str(refused.value)


@pytest.mark.parametrize("host", [None, ""])
def test_demo_refuses_a_missing_bind_host(host):
    with pytest.raises(ConfigError, match="SAFE_BIND_HOST is required"):
        load_settings(demo_env(SAFE_BIND_HOST=host))


def test_demo_refuses_a_missing_dataset_marker():
    with pytest.raises(ConfigError, match="synthetic-demo"):
        load_settings(demo_env(SAFE_DATASET=None))


@pytest.mark.parametrize("marker", ["real-cases", "SYNTHETIC-DEMO", ""])
def test_demo_refuses_any_marker_other_than_synthetic_demo(marker):
    with pytest.raises(ConfigError, match="synthetic-demo"):
        load_settings(demo_env(SAFE_DATASET=marker))


def test_missing_mode_is_refused():
    with pytest.raises(ConfigError, match="SAFE_MODE"):
        load_settings(demo_env(SAFE_MODE=None))


@pytest.mark.parametrize("mode", ["Demo", "DEMO", "prod", "test", "", " demo", "demo "])
def test_unknown_mode_is_refused(mode):
    with pytest.raises(ConfigError, match="SAFE_MODE"):
        load_settings(demo_env(SAFE_MODE=mode))


@pytest.mark.parametrize("name", sorted(DB_SETTINGS))
def test_each_database_setting_is_required(name):
    with pytest.raises(ConfigError, match=name):
        load_settings(demo_env(**{name: None}))


@pytest.mark.parametrize("name", ["SAFE_DB_HOST", "SAFE_DB_NAME", "SAFE_DB_USER", "SAFE_DB_PASSWORD_FILE"])
def test_empty_database_setting_is_refused(name):
    with pytest.raises(ConfigError, match=name):
        load_settings(production_env(**{name: "   "}))


@pytest.mark.parametrize("port", ["", "abc", "0", "65536", "-1", "54 32"])
def test_database_port_must_be_a_port_number(port):
    with pytest.raises(ConfigError, match="SAFE_DB_PORT"):
        load_settings(production_env(SAFE_DB_PORT=port))


@pytest.mark.parametrize(
    "env",
    [
        demo_env(SAFE_MODE=SECRET),
        demo_env(SAFE_BIND_HOST=SECRET),
        demo_env(SAFE_DATASET=SECRET),
        production_env(SAFE_DATASET=SECRET),
        production_env(SAFE_DB_PORT=SECRET),
    ],
)
def test_error_messages_never_echo_a_value(env):
    with pytest.raises(ConfigError) as info:
        load_settings(env)

    assert SECRET not in str(info.value)


def test_guard_does_not_read_the_password_file():
    # The path may point at nothing: the guard checks that the setting is present, never the file it names.
    assert load_settings(demo_env(SAFE_DB_PASSWORD_FILE="/nonexistent/path/to/password")).db_password_file


def test_connect_refuses_a_missing_setting_instead_of_connecting_to_a_default(monkeypatch, tmp_path):
    password_file = tmp_path / "password.txt"
    password_file.write_text(SECRET, encoding="utf-8")
    for name, value in DB_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SAFE_DB_PASSWORD_FILE", str(password_file))

    def no_connection(*args, **kwargs):
        raise AssertionError("a database connection was attempted")

    monkeypatch.setattr(psycopg, "connect", no_connection)
    for name in ("SAFE_DB_HOST", "SAFE_DB_PORT", "SAFE_DB_NAME", "SAFE_DB_USER"):
        monkeypatch.delenv(name)
        with pytest.raises(ConfigError, match=name):
            db.connect()
        monkeypatch.setenv(name, DB_SETTINGS[name])


def test_api_startup_refuses_an_invalid_environment(monkeypatch):
    for name in ("SAFE_MODE", "SAFE_BIND_HOST", "SAFE_DATASET", *DB_SETTINGS):
        monkeypatch.delenv(name, raising=False)

    async def start():
        async with api.app.router.lifespan_context(api.app):
            pass

    with pytest.raises(ConfigError, match="SAFE_MODE"):
        asyncio.run(start())


def test_api_startup_accepts_a_valid_environment(monkeypatch):
    for name, value in demo_env().items():
        monkeypatch.setenv(name, value)

    async def start():
        async with api.app.router.lifespan_context(api.app):
            pass

    asyncio.run(start())


def test_worker_process_refuses_to_start_without_a_valid_environment():
    # Run the real entrypoint in a clean environment that holds no database settings: it must exit before connecting.
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SOURCE_DIR)}
    result = subprocess.run(
        [sys.executable, "-m", "safe_kit.worker"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2
    assert "SAFE_MODE" in result.stderr
    assert SECRET not in result.stderr
