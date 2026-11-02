"""Demo dataset and demo sign-in (F2 package P12; plans/06 demo security boundary, TM-02 and TM-33).

The tests that need no database cover the refusals: the seed refuses outside demo mode, the credential path is
required, demo sign-in is refused in production mode and for non-loopback callers, and the repository holds no default
demo credential. The creation path runs against a fake connection, so the credential file mode, the stored hash and
the silence of stdout are checked without a database. The tests marked "database" need SAFE_TEST_DATABASE=1 and the
SAFE_DB_* settings; they seed the throwaway test database once and check the rest against that seed.
"""

import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from safe_kit import api, demo_seed
from safe_kit.config_guard import ConfigError, is_demo
from safe_kit.db import connect
from safe_kit.demo_auth import credential_matches
from safe_kit.migrate import main as migrate
from safe_kit.security import verify_password

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_ENABLED = os.environ.get("SAFE_TEST_DATABASE") == "1"
requires_database = pytest.mark.skipif(
    not DATABASE_ENABLED,
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

DB_KEYS = ("SAFE_DB_HOST", "SAFE_DB_PORT", "SAFE_DB_NAME", "SAFE_DB_USER", "SAFE_DB_PASSWORD_FILE")
DB_DEFAULTS = {
    "SAFE_DB_HOST": "127.0.0.1",
    "SAFE_DB_PORT": "5432",
    "SAFE_DB_NAME": "safe",
    "SAFE_DB_USER": "safe_app",
    "SAFE_DB_PASSWORD_FILE": "/nonexistent/password",
}
TOKEN_TTL = {"SAFE_ACCESS_TTL_MINUTES": "60", "SAFE_REFRESH_TTL_MINUTES": "60", "SAFE_SESSION_MAX_HOURS": "12"}
LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.9", 50000)
GATEWAY = "172.28.0.10"  # the gateway's fixed address on the edge network (compose.yaml)


def _db_settings() -> dict[str, str]:
    # The database tests use the database the test script started; the other tests never connect.
    return {key: os.environ.get(key, DB_DEFAULTS[key]) for key in DB_KEYS}


def demo_env(**changes: str | None) -> dict[str, str]:
    env = {"SAFE_MODE": "demo", "SAFE_BIND_HOST": "127.0.0.1", "SAFE_DATASET": "synthetic-demo"}
    env.update(_db_settings())
    env.update(TOKEN_TTL)
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def production_env() -> dict[str, str]:
    env = {"SAFE_MODE": "production"}
    env.update(_db_settings())
    env.update(TOKEN_TTL)
    return env


@contextmanager
def process_env(env: dict[str, str]) -> Iterator[None]:
    """Replaces every SAFE_* variable of the process with env, and restores the process environment on exit."""
    with patch.dict(os.environ, {}):
        for key in [key for key in os.environ if key.startswith("SAFE_")]:
            del os.environ[key]
        os.environ.update(env)
        yield


class _FakeResult:
    def fetchone(self):
        return None


class _FakeConnection:
    def execute(self, query, params=None):
        return _FakeResult()


class _FakeConnect:
    def __enter__(self):
        return _FakeConnection()

    def __exit__(self, *exc_info):
        return False


# --- refusals and guard rules (no database) -----------------------------------------------------------------------


def test_seed_refuses_production_mode_before_touching_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_seed, "connect", lambda: pytest.fail("the seed must not connect in production mode"))
    path = tmp_path / "credential"
    env = production_env() | {demo_seed.CREDENTIAL_FILE_ENV: str(path)}
    with pytest.raises(demo_seed.SeedRefused):
        demo_seed.seed(env)
    assert not path.exists()


@pytest.mark.parametrize("dataset", [None, "", "customer-data", "Synthetic-Demo"])
def test_seed_refuses_any_dataset_but_the_synthetic_marker(tmp_path, monkeypatch, dataset):
    monkeypatch.setattr(demo_seed, "connect", lambda: pytest.fail("the seed must not connect"))
    env = demo_env(SAFE_DATASET=dataset) | {demo_seed.CREDENTIAL_FILE_ENV: str(tmp_path / "credential")}
    with pytest.raises(ConfigError):
        demo_seed.seed(env)
    assert not (tmp_path / "credential").exists()


def test_seed_requires_the_credential_path_and_has_no_default(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_seed, "connect", lambda: pytest.fail("the seed must not connect without a path"))
    for value in (None, "", "   "):
        env = demo_env() | {demo_seed.CREDENTIAL_FILE_ENV: value} if value is not None else demo_env()
        with pytest.raises(demo_seed.SeedError, match="SAFE_DEMO_CREDENTIAL_FILE"):
            demo_seed.seed(env)


def test_demo_flag_follows_the_startup_guard():
    assert is_demo(demo_env())
    assert not is_demo(production_env())
    assert not is_demo(demo_env(SAFE_BIND_HOST="0.0.0.0"))
    assert not is_demo(demo_env(SAFE_DATASET="customer-data"))


def test_demo_status_is_false_in_production_and_true_in_demo():
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(production_env()):
        assert client.get("/api/v1/demo/status").json() == {"demo": False}
    with process_env(demo_env()):
        assert client.get("/api/v1/demo/status").json() == {"demo": True}


def test_demo_sign_in_is_refused_in_production_mode_without_reading_the_database(monkeypatch):
    monkeypatch.setattr(api, "credential_matches", lambda credential: pytest.fail("production must not check it"))
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(production_env()):
        response = client.post("/api/v1/demo/sign-in", json={"credential": "anything-at-all"})
    assert response.status_code == 403
    assert "safe_access" not in response.cookies


def test_demo_sign_in_is_refused_for_a_non_loopback_caller(monkeypatch):
    monkeypatch.setattr(api, "credential_matches", lambda credential: pytest.fail("a remote caller must not reach it"))
    client = TestClient(api.app, client=REMOTE)
    with process_env(demo_env()):
        response = client.post("/api/v1/demo/sign-in", json={"credential": "anything-at-all"})
    assert response.status_code == 403


@pytest.mark.parametrize("trusted", [None, GATEWAY])
def test_demo_sign_in_is_refused_for_a_forged_loopback_claim_from_an_untrusted_peer(monkeypatch, trusted):
    """A client that sends X-Forwarded-For: 127.0.0.1 itself is not on loopback, with or without a trusted gateway."""
    monkeypatch.setattr(api, "credential_matches", lambda credential: pytest.fail("a forged caller must not reach it"))
    monkeypatch.setattr(api, "record_login_failure", lambda *args: pytest.fail("a forged caller is not audited here"))
    client = TestClient(api.app, client=REMOTE)
    env = demo_env(SAFE_TRUSTED_PROXIES=trusted)
    with process_env(env):
        response = client.post(
            "/api/v1/demo/sign-in",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
            json={"credential": "anything-at-all"},
        )
    assert response.status_code == 403


def test_demo_sign_in_passes_the_gate_for_loopback_forwarded_by_the_trusted_gateway(monkeypatch):
    """Without a database: a wrong credential from the resolved loopback address reaches the credential check."""
    audited = []
    monkeypatch.setattr(api, "hit_rate_limit", lambda key, window: 1)
    monkeypatch.setattr(api, "dataset_credential_hash", lambda: "stored-hash")
    monkeypatch.setattr(api, "credential_matches", lambda credential: False)
    monkeypatch.setattr(api, "record_login_failure", lambda *args: audited.append(args))
    client = TestClient(api.app, client=(GATEWAY, 50000))
    with process_env(demo_env(SAFE_TRUSTED_PROXIES=GATEWAY)):
        response = client.post(
            "/api/v1/demo/sign-in",
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"credential": "wrong-credential"},
        )
    assert response.status_code == 401
    assert audited == [(api.DEMO_ADMIN_ID, "wrong_password", api.DEMO_ADMIN_USERNAME, "127.0.0.1")]


@pytest.mark.parametrize("peer", [LOOPBACK, (GATEWAY, 50000)])
def test_production_mode_refuses_demo_sign_in_even_through_the_trusted_gateway(monkeypatch, peer):
    monkeypatch.setattr(api, "credential_matches", lambda credential: pytest.fail("production must not check it"))
    client = TestClient(api.app, client=peer)
    with process_env(production_env() | {"SAFE_TRUSTED_PROXIES": GATEWAY}):
        response = client.post(
            "/api/v1/demo/sign-in",
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"credential": "anything-at-all"},
        )
    assert response.status_code == 403
    assert "safe_access" not in response.cookies


def test_creation_path_writes_one_0600_file_and_prints_nothing(tmp_path, monkeypatch, capsys, caplog):
    """The first run: a fake connection stands in for an empty database, so the file and the hash are checked directly."""
    stored = {}
    monkeypatch.setattr(demo_seed, "connect", lambda: _FakeConnect())
    monkeypatch.setattr(demo_seed, "storage_from_env", lambda: None)
    monkeypatch.setattr(demo_seed, "key_provider_from_env", lambda: None)
    monkeypatch.setattr(demo_seed, "_insert_dataset", lambda conn, credential_hash, storage, provider: stored.update(h=credential_hash))
    path = tmp_path / "demo-credential"
    env = demo_env() | {demo_seed.CREDENTIAL_FILE_ENV: str(path)}

    assert demo_seed.seed(env).created is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    credential = path.read_text(encoding="utf-8").strip()
    assert verify_password(stored["h"], credential)
    captured = capsys.readouterr()
    assert credential not in captured.out + captured.err
    assert credential not in caplog.text


def test_creation_path_never_overwrites_and_removes_its_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_seed, "connect", lambda: _FakeConnect())
    monkeypatch.setattr(demo_seed, "storage_from_env", lambda: None)
    monkeypatch.setattr(demo_seed, "key_provider_from_env", lambda: None)
    path = tmp_path / "demo-credential"
    env = demo_env() | {demo_seed.CREDENTIAL_FILE_ENV: str(path)}

    path.write_text("an earlier credential\n", encoding="utf-8")
    with pytest.raises(demo_seed.SeedError, match="never overwrites"):
        demo_seed.seed(env)
    assert path.read_text(encoding="utf-8") == "an earlier credential\n"

    path.unlink()

    def failing_insert(conn, credential_hash, storage, provider):
        raise RuntimeError("simulated failure after the file was written")

    monkeypatch.setattr(demo_seed, "_insert_dataset", failing_insert)
    with pytest.raises(RuntimeError):
        demo_seed.seed(env)
    assert not path.exists()


def test_repository_holds_no_default_demo_credential():
    pattern = re.compile(
        r"(?i)(demo[_-]?(password|passwd|credential)\s*[:=]\s*[\"'][^\"']+[\"']|changeme|demo-password|demo_password)"
    )
    roots = [REPO_ROOT / "backend" / "src", REPO_ROOT / "backend" / "migrations", REPO_ROOT / "frontend" / "src",
             REPO_ROOT / "db", REPO_ROOT / "gateway", REPO_ROOT / "scripts"]
    files = [REPO_ROOT / "compose.yaml"]
    for root in roots:
        files.extend(path for path in root.rglob("*") if path.is_file() and "node_modules" not in path.parts)
    offenders = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


# --- database tests -----------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo_dataset(tmp_path_factory) -> Iterator[dict]:
    """Seeds the throwaway database once for this module. Its process environment is the demo environment."""
    if not DATABASE_ENABLED:
        pytest.skip("set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests")
    work = tmp_path_factory.mktemp("demo")
    kek = work / "kek"
    kek.write_bytes(secrets.token_bytes(32))
    os.chmod(kek, 0o600)
    (work / "evidence").mkdir()
    jwt_key = work / "jwt-key"
    jwt_key.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    credential_path = work / "credential"
    env = demo_env() | {
        demo_seed.CREDENTIAL_FILE_ENV: str(credential_path),
        "SAFE_EVIDENCE_DIR": str(work / "evidence"),
        "SAFE_EVIDENCE_KEK_FILE": str(kek),
        "SAFE_JWT_KEY_FILE": str(jwt_key),
    }
    with patch.dict(os.environ, env, clear=False):
        migrate()
        outcome = demo_seed.seed(os.environ)
        yield {"created": outcome.created, "path": credential_path, "env": env}


def _table_counts() -> dict[str, int]:
    with connect() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
        return {
            table: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            for table in tables
        }


def _credential(demo_dataset) -> str | None:
    if not demo_dataset["path"].exists():
        return None
    return demo_dataset["path"].read_text(encoding="utf-8").strip()


@requires_database
def test_seed_writes_the_fixed_dataset(demo_dataset):
    with connect() as conn:
        demo_users = conn.execute("SELECT count(*) FROM users WHERE username LIKE 'demo-%' AND NOT is_deleted").fetchone()[0]
        roles = {row[0] for row in conn.execute(
            "SELECT role FROM users WHERE username LIKE 'demo-%' AND NOT is_deleted").fetchall()}
        cases = conn.execute(
            "SELECT count(*) FROM cases WHERE id IN (%s, %s, %s) AND NOT is_deleted",
            (demo_seed.CASE_ONE, demo_seed.CASE_TWO, demo_seed.CASE_THREE),
        ).fetchone()[0]
        layers = conn.execute(
            "SELECT count(*) FROM review_layers WHERE round_id = %s AND NOT is_deleted", (demo_seed.ROUND_ONE,)
        ).fetchone()[0]
        approvals = conn.execute(
            "SELECT count(*) FROM disclosure_approvals WHERE version_id = %s", (demo_seed.DISCLOSURE_VERSION_ONE,)
        ).fetchone()[0]
        marker = conn.execute(
            "SELECT count(*) FROM demo_dataset WHERE dataset = 'synthetic-demo' AND NOT is_deleted"
        ).fetchone()[0]
    assert demo_users == len(demo_seed.USERS) == 8
    assert roles == {role for _, _, role, *_ in demo_seed.USERS}
    assert cases == 3
    assert layers == 11
    assert approvals == 0
    assert marker == 1


@requires_database
def test_second_seed_creates_no_rows(demo_dataset):
    before = _table_counts()
    with patch.dict(os.environ, demo_dataset["env"], clear=False):
        outcome = demo_seed.seed(os.environ)
    after = _table_counts()
    assert outcome.created is False
    assert after == before


@requires_database
def test_credential_file_is_0600_holds_one_line_and_matches_the_stored_hash(demo_dataset):
    if not demo_dataset["created"]:
        pytest.skip("the demo dataset was already in this database; use a freshly migrated database")
    path = demo_dataset["path"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert credential_matches(lines[0])


@requires_database
def test_credential_is_not_printed_or_logged(demo_dataset, capsys, caplog):
    credential = _credential(demo_dataset)
    with patch.dict(os.environ, demo_dataset["env"], clear=False):
        assert demo_seed.main(["seed"]) == 0
    captured = capsys.readouterr()
    assert "already seeded" in captured.out
    if credential is not None:
        assert credential not in captured.out + captured.err
    assert credential is None or credential not in caplog.text


@requires_database
def test_demo_sign_in_accepts_the_demo_credential_on_loopback(demo_dataset):
    credential = _credential(demo_dataset)
    if credential is None:
        pytest.skip("the demo dataset was already in this database; the credential file is not available")
    client = TestClient(api.app, client=LOOPBACK)
    with patch.dict(os.environ, demo_dataset["env"], clear=False):
        wrong = client.post("/api/v1/demo/sign-in", json={"credential": credential + "x"})
        right = client.post("/api/v1/demo/sign-in", json={"credential": credential})
    assert wrong.status_code == 401
    assert right.status_code == 200
    body = right.json()
    assert body["password_change_required"] is False
    assert body["access_token"]
    assert "safe_access" in right.cookies


@requires_database
def test_demo_sign_in_accepts_loopback_forwarded_by_the_trusted_gateway_only(demo_dataset):
    credential = _credential(demo_dataset)
    if credential is None:
        pytest.skip("the demo dataset was already in this database; the credential file is not available")
    gateway = TestClient(api.app, client=(GATEWAY, 50000))
    with patch.dict(os.environ, demo_dataset["env"] | {"SAFE_TRUSTED_PROXIES": GATEWAY}, clear=False):
        accepted = gateway.post(
            "/api/v1/demo/sign-in", headers={"X-Forwarded-For": "127.0.0.1"}, json={"credential": credential}
        )
        # The same header from an address that is not a trusted proxy is ignored.
        forged = TestClient(api.app, client=REMOTE).post(
            "/api/v1/demo/sign-in", headers={"X-Forwarded-For": "127.0.0.1"}, json={"credential": credential}
        )
    assert accepted.status_code == 200
    assert "safe_access" in accepted.cookies
    assert forged.status_code == 403


@requires_database
def test_demo_sign_in_is_refused_for_a_remote_caller_even_with_the_right_credential(demo_dataset):
    credential = _credential(demo_dataset)
    if credential is None:
        pytest.skip("the demo dataset was already in this database; the credential file is not available")
    client = TestClient(api.app, client=REMOTE)
    with patch.dict(os.environ, demo_dataset["env"], clear=False):
        response = client.post("/api/v1/demo/sign-in", json={"credential": credential})
    assert response.status_code == 403


@requires_database
def test_production_mode_never_accepts_the_demo_credential(demo_dataset):
    credential = _credential(demo_dataset)
    if credential is None:
        pytest.skip("the demo dataset was already in this database; the credential file is not available")
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(production_env()):
        response = client.post("/api/v1/demo/sign-in", json={"credential": credential})
    assert response.status_code == 403
    assert "safe_access" not in response.cookies
