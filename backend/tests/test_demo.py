"""Demo dataset, per-role demo credentials and demo sign-in (F2 package P12; plans/06, TM-02 and TM-33).

The tests that need no database cover the refusals (seed and upgrade outside demo mode, the credential directory is
required, demo sign-in refused in production mode and for non-loopback callers), the roster, the creation paths against
a fake connection (file mode, O_EXCL, cleanup on failure, silent output), and the repository holding no default
credential. The tests marked "database" need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings. They seed the throwaway
test database once (both seed versions) and check the rest against that seed. The upgrade test builds a version-1
database of its own, runs migration 0046 on it, and then runs the upgrade.
"""

import json
import os
import re
import secrets
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from safe_kit import api, demo_seed
from safe_kit import migrate as migrate_module
from safe_kit.config_guard import ConfigError, is_demo
from safe_kit.db import connect
from safe_kit.demo_auth import DEMO_ADMIN_ID, DEMO_ADMIN_USERNAME, DEMO_ROLES, demo_credential_hash
from safe_kit.evidence_keys import key_provider_from_env
from safe_kit.evidence_storage import storage_from_env
from safe_kit.migrate import main as migrate
from safe_kit.security import hash_password, verify_password

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "backend" / "migrations"
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
SIGN_IN = "/api/v1/demo/sign-in"
CREDENTIAL_DIR = demo_seed.CREDENTIAL_DIR_ENV
CREDENTIAL_FILE = demo_seed.CREDENTIAL_FILE_ENV
ROLE_USERNAMES = [demo_role.username for demo_role in DEMO_ROLES]
SEEDED_CASES = (demo_seed.CASE_ONE, demo_seed.CASE_TWO, demo_seed.CASE_THREE)


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


def _fake_database(monkeypatch) -> None:
    monkeypatch.setattr(demo_seed, "connect", lambda: _FakeConnect())
    monkeypatch.setattr(demo_seed, "storage_from_env", lambda: None)
    monkeypatch.setattr(demo_seed, "key_provider_from_env", lambda: None)


# --- refusals, roster and guard rules (no database) ---------------------------------------------------------------


@pytest.mark.parametrize("run", [demo_seed.seed, demo_seed.upgrade])
def test_seed_and_upgrade_refuse_production_mode_before_touching_anything(tmp_path, monkeypatch, run):
    monkeypatch.setattr(demo_seed, "connect", lambda: pytest.fail("must not connect in production mode"))
    credential_dir = tmp_path / "credentials"
    env = production_env() | {CREDENTIAL_DIR: str(credential_dir)}
    with pytest.raises(demo_seed.SeedRefused):
        run(env)
    assert not credential_dir.exists()


@pytest.mark.parametrize("run", [demo_seed.seed, demo_seed.upgrade])
@pytest.mark.parametrize("dataset", [None, "", "customer-data", "Synthetic-Demo"])
def test_seed_and_upgrade_refuse_any_dataset_but_the_synthetic_marker(tmp_path, monkeypatch, run, dataset):
    monkeypatch.setattr(demo_seed, "connect", lambda: pytest.fail("must not connect"))
    env = demo_env(SAFE_DATASET=dataset) | {CREDENTIAL_DIR: str(tmp_path / "credentials")}
    with pytest.raises(ConfigError):
        run(env)
    assert not (tmp_path / "credentials").exists()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_seed_requires_the_credential_directory_and_has_no_default(tmp_path, monkeypatch, value):
    """The single-file setting alone is not enough: the other demo roles need the directory."""
    monkeypatch.setattr(demo_seed, "connect", lambda: pytest.fail("must not connect without a directory"))
    env = demo_env(**{CREDENTIAL_DIR: value, CREDENTIAL_FILE: str(tmp_path / "legacy-credential")})
    with pytest.raises(demo_seed.SeedError, match="SAFE_DEMO_CREDENTIAL_DIR"):
        demo_seed.seed(env)
    assert not (tmp_path / "legacy-credential").exists()


def test_demo_flag_follows_the_startup_guard():
    assert is_demo(demo_env())
    assert not is_demo(production_env())
    assert not is_demo(demo_env(SAFE_BIND_HOST="0.0.0.0"))
    assert not is_demo(demo_env(SAFE_DATASET="customer-data"))


def test_demo_status_lists_the_demo_roles_only_in_demo_mode():
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(production_env()):
        assert client.get("/api/v1/demo/status").json() == {"demo": False, "users": []}
    with process_env(demo_env()):
        body = client.get("/api/v1/demo/status").json()
    assert body["demo"] is True
    assert [user["username"] for user in body["users"]] == ROLE_USERNAMES
    for user in body["users"]:
        assert set(user) == {"username", "name", "role"}


def test_roster_matches_the_seeded_users():
    seeded = {username: (role, f"{first} {last}") for _, username, role, first, last, _ in demo_seed.USERS}
    for demo_role in DEMO_ROLES:
        assert seeded[demo_role.username] == (demo_role.role, demo_role.name)


def test_demo_sign_in_is_refused_in_production_mode_without_reading_the_database(monkeypatch):
    monkeypatch.setattr(api, "find_user", lambda username: pytest.fail("production must not look up a user"))
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(production_env()):
        response = client.post(SIGN_IN, json={"username": "demo-admin", "credential": "anything-at-all"})
    assert response.status_code == 403
    assert "safe_access" not in response.cookies


def test_demo_sign_in_is_refused_for_a_non_loopback_caller(monkeypatch):
    monkeypatch.setattr(api, "find_user", lambda username: pytest.fail("a remote caller must not reach it"))
    client = TestClient(api.app, client=REMOTE)
    with process_env(demo_env()):
        response = client.post(SIGN_IN, json={"username": "demo-admin", "credential": "anything-at-all"})
    assert response.status_code == 403


@pytest.mark.parametrize("trusted", [None, GATEWAY])
def test_demo_sign_in_is_refused_for_a_forged_loopback_claim_from_an_untrusted_peer(monkeypatch, trusted):
    """A client that sends X-Forwarded-For: 127.0.0.1 itself is not on loopback, with or without a trusted gateway."""
    monkeypatch.setattr(api, "find_user", lambda username: pytest.fail("a forged caller must not reach it"))
    monkeypatch.setattr(api, "record_login_failure", lambda *args: pytest.fail("a forged caller is not audited here"))
    client = TestClient(api.app, client=REMOTE)
    with process_env(demo_env(SAFE_TRUSTED_PROXIES=trusted)):
        response = client.post(
            SIGN_IN,
            headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
            json={"username": "demo-admin", "credential": "anything-at-all"},
        )
    assert response.status_code == 403


def test_demo_sign_in_checks_the_credential_for_a_roster_user_through_the_trusted_gateway(monkeypatch):
    """Without a database: a wrong credential for a roster user from the resolved loopback address is a 401 and is audited."""
    audited = []
    user = SimpleNamespace(id="5afe0d00-0000-4000-8000-000000000003", is_active=True)
    monkeypatch.setattr(api, "hit_rate_limit", lambda key, window: 1)
    monkeypatch.setattr(api, "find_user", lambda username: user)
    monkeypatch.setattr(api, "demo_credential_hash", lambda user_id: None)
    monkeypatch.setattr(api, "record_login_failure", lambda *args: audited.append(args))
    client = TestClient(api.app, client=(GATEWAY, 50000))
    with process_env(demo_env(SAFE_TRUSTED_PROXIES=GATEWAY)):
        response = client.post(
            SIGN_IN,
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"username": "demo-triager", "credential": "wrong-credential"},
        )
    assert response.status_code == 401
    assert audited == [(user.id, "wrong_password", "demo-triager", "127.0.0.1")]


def test_demo_sign_in_refuses_a_username_outside_the_demo_roles_without_a_lookup(monkeypatch):
    monkeypatch.setattr(api, "hit_rate_limit", lambda key, window: 1)
    monkeypatch.setattr(api, "find_user", lambda username: pytest.fail("a non-demo username must not be looked up"))
    audited = []
    monkeypatch.setattr(api, "record_login_failure", lambda *args: audited.append(args))
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(demo_env()):
        response = client.post(SIGN_IN, json={"username": "example-user", "credential": "anything-at-all"})
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}
    assert audited == [(None, "unknown_user", "example-user", "127.0.0.1")]


@pytest.mark.parametrize("peer", [LOOPBACK, (GATEWAY, 50000)])
def test_production_mode_refuses_demo_sign_in_even_through_the_trusted_gateway(monkeypatch, peer):
    monkeypatch.setattr(api, "find_user", lambda username: pytest.fail("production must not look up a user"))
    client = TestClient(api.app, client=peer)
    with process_env(production_env() | {"SAFE_TRUSTED_PROXIES": GATEWAY}):
        response = client.post(
            SIGN_IN,
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"username": "demo-admin", "credential": "anything-at-all"},
        )
    assert response.status_code == 403
    assert "safe_access" not in response.cookies


# --- creation paths against a fake connection (no database) -------------------------------------------------------


def test_fresh_seed_writes_the_admin_file_with_mode_0600_and_prints_nothing(tmp_path, monkeypatch, capsys, caplog):
    _fake_database(monkeypatch)
    recorded = {}
    monkeypatch.setattr(
        demo_seed, "_insert_dataset", lambda conn, credential_hash, storage, provider: recorded.update(hash=credential_hash)
    )
    monkeypatch.setattr(demo_seed, "_apply_version_2", lambda conn, credential_dir, written: recorded.update(v2=True))
    credential_dir = tmp_path / "credentials"
    env = demo_env(**{CREDENTIAL_DIR: str(credential_dir)})

    outcome = demo_seed.seed(env)

    assert outcome.created is True
    assert outcome.applied == (1, 2)
    assert recorded["v2"] is True
    path = credential_dir / f"{DEMO_ADMIN_USERNAME}{demo_seed.CREDENTIAL_SUFFIX}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    credential = path.read_text(encoding="utf-8").strip()
    assert verify_password(recorded["hash"], credential)
    captured = capsys.readouterr()
    assert credential not in captured.out + captured.err
    assert credential not in caplog.text


def test_the_single_file_setting_still_names_the_admin_file(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    monkeypatch.setattr(demo_seed, "_insert_dataset", lambda *args: None)
    monkeypatch.setattr(demo_seed, "_apply_version_2", lambda *args: None)
    credential_dir = tmp_path / "credentials"
    legacy = tmp_path / "legacy-credential"
    demo_seed.seed(demo_env(**{CREDENTIAL_DIR: str(credential_dir), CREDENTIAL_FILE: str(legacy)}))
    assert legacy.exists()
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
    assert not (credential_dir / f"{DEMO_ADMIN_USERNAME}{demo_seed.CREDENTIAL_SUFFIX}").exists()


def test_seed_never_overwrites_an_existing_credential_file(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    monkeypatch.setattr(demo_seed, "_insert_dataset", lambda *args: pytest.fail("nothing must be written"))
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir()
    path = credential_dir / f"{DEMO_ADMIN_USERNAME}{demo_seed.CREDENTIAL_SUFFIX}"
    path.write_text("an earlier credential\n", encoding="utf-8")
    with pytest.raises(demo_seed.SeedError, match="never overwrites"):
        demo_seed.seed(demo_env(**{CREDENTIAL_DIR: str(credential_dir)}))
    assert path.read_text(encoding="utf-8") == "an earlier credential\n"


def test_failed_seed_removes_the_admin_file_it_wrote(tmp_path, monkeypatch):
    _fake_database(monkeypatch)

    def failing_insert(conn, credential_hash, storage, provider):
        raise RuntimeError("simulated failure after the file was written")

    monkeypatch.setattr(demo_seed, "_insert_dataset", failing_insert)
    credential_dir = tmp_path / "credentials"
    with pytest.raises(RuntimeError):
        demo_seed.seed(demo_env(**{CREDENTIAL_DIR: str(credential_dir)}))
    assert list(credential_dir.iterdir()) == []


def test_failed_version_2_removes_the_files_it_wrote_in_this_run(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    monkeypatch.setattr(demo_seed, "_marker_version", lambda conn: 1)

    def failing_version_2(conn, credential_dir, written):
        demo_seed._new_credential(os.path.join(credential_dir, "demo-reporter" + demo_seed.CREDENTIAL_SUFFIX), written)
        raise RuntimeError("simulated failure in version 2")

    monkeypatch.setattr(demo_seed, "_apply_version_2", failing_version_2)
    credential_dir = tmp_path / "credentials"
    with pytest.raises(RuntimeError):
        demo_seed.upgrade(demo_env(**{CREDENTIAL_DIR: str(credential_dir)}))
    assert list(credential_dir.iterdir()) == []


def test_upgrade_refuses_a_database_without_the_dataset(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    credential_dir = tmp_path / "credentials"
    with pytest.raises(demo_seed.SeedError, match="no demo dataset"):
        demo_seed.upgrade(demo_env(**{CREDENTIAL_DIR: str(credential_dir)}))
    assert not credential_dir.exists() or list(credential_dir.iterdir()) == []


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


def _support_files(work: Path) -> dict[str, str]:
    kek = work / "kek"
    kek.write_bytes(secrets.token_bytes(32))
    os.chmod(kek, 0o600)
    evidence = work / "evidence"
    evidence.mkdir()
    jwt_key = work / "jwt-key"
    jwt_key.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    return {"SAFE_EVIDENCE_DIR": str(evidence), "SAFE_EVIDENCE_KEK_FILE": str(kek), "SAFE_JWT_KEY_FILE": str(jwt_key)}


@pytest.fixture(scope="module")
def demo_dataset(tmp_path_factory) -> Iterator[dict]:
    """Seeds the throwaway database once for this module (both seed versions). Its process environment is the demo one."""
    if not DATABASE_ENABLED:
        pytest.skip("set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests")
    work = tmp_path_factory.mktemp("demo")
    credential_dir = work / "credentials"
    env = demo_env(**{CREDENTIAL_DIR: str(credential_dir)}) | _support_files(work)
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop(CREDENTIAL_FILE, None)
        migrate()
        outcome = demo_seed.seed(os.environ)
        yield {"outcome": outcome, "dir": credential_dir, "env": env}


def _table_counts() -> dict[str, int]:
    with connect() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
        return {
            table: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            for table in tables
        }


def _credential_files(directory: Path) -> dict[str, Path]:
    suffix = demo_seed.CREDENTIAL_SUFFIX
    return {path.name[: -len(suffix)]: path for path in directory.glob("*" + suffix)}


def _directory_snapshot(directory: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in directory.iterdir()}


def _credential(dataset: dict, username: str) -> str:
    return (dataset["dir"] / f"{username}{demo_seed.CREDENTIAL_SUFFIX}").read_text(encoding="utf-8").strip()


def _stored_hash(username: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.credential_hash
            FROM demo_user_credentials c
            JOIN users u ON u.id = c.user_id
            WHERE u.username = %s AND NOT u.is_deleted AND NOT c.is_deleted
            """,
            (username,),
        ).fetchone()
    return row[0] if row else None


def _marker_version() -> int:
    with connect() as conn:
        return conn.execute("SELECT seed_version FROM demo_dataset WHERE NOT is_deleted").fetchone()[0]


@requires_database
def test_seed_creates_one_user_per_demo_role_with_its_own_credential(demo_dataset):
    with connect() as conn:
        users = dict(conn.execute("SELECT username, role FROM users WHERE username LIKE 'demo-%' AND NOT is_deleted").fetchall())
        credentialed = {row[0] for row in conn.execute(
            """
            SELECT u.username FROM demo_user_credentials c JOIN users u ON u.id = c.user_id
            WHERE NOT c.is_deleted
            """
        ).fetchall()}
        cases = conn.execute(
            "SELECT count(*) FROM cases WHERE id IN (%s, %s, %s) AND NOT is_deleted", SEEDED_CASES
        ).fetchone()[0]
        layers = conn.execute(
            "SELECT count(*) FROM review_layers WHERE round_id = %s AND NOT is_deleted", (demo_seed.ROUND_ONE,)
        ).fetchone()[0]
        approvals = conn.execute(
            "SELECT count(*) FROM disclosure_approvals WHERE version_id = %s", (demo_seed.DISCLOSURE_VERSION_ONE,)
        ).fetchone()[0]
    assert users == {**{r.username: r.role for r in DEMO_ROLES}, "demo-reporting-owner": "reporting_owner"}
    assert credentialed == set(ROLE_USERNAMES)
    assert cases == 3
    assert layers == 11
    assert approvals == 0
    assert _marker_version() == demo_seed.SEED_VERSION == 2


@requires_database
def test_seed_writes_one_0600_credential_file_per_demo_role(demo_dataset):
    files = _credential_files(demo_dataset["dir"])
    assert set(files) == set(ROLE_USERNAMES)
    hashes = set()
    for username, path in files.items():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert verify_password(_stored_hash(username), lines[0])
        hashes.add(_stored_hash(username))
    assert len(hashes) == len(ROLE_USERNAMES)


@requires_database
def test_second_run_of_either_version_creates_no_rows_and_no_files(demo_dataset, capsys, caplog):
    before_counts = _table_counts()
    before_files = _directory_snapshot(demo_dataset["dir"])
    with patch.dict(os.environ, demo_dataset["env"], clear=False):
        seeded = demo_seed.seed(os.environ)
        upgraded = demo_seed.upgrade(os.environ)
        assert demo_seed.main(["seed"]) == 0
        assert demo_seed.main(["upgrade"]) == 0
    captured = capsys.readouterr()
    assert seeded.created is False
    assert upgraded.created is False
    assert _table_counts() == before_counts
    assert _directory_snapshot(demo_dataset["dir"]) == before_files
    assert "already seeded" in captured.out
    for username in ROLE_USERNAMES:
        credential = _credential(demo_dataset, username)
        assert credential not in captured.out + captured.err
        assert credential not in caplog.text


@requires_database
def test_upgrade_on_a_version_1_database_adds_the_credentials_once(tmp_path_factory):
    work = tmp_path_factory.mktemp("upgrade")
    db_name = f"safe_demo_upgrade_{uuid.uuid4().hex[:8]}"
    host = _db_settings()
    with psycopg.connect(
        host=host["SAFE_DB_HOST"],
        port=int(host["SAFE_DB_PORT"]),
        dbname="postgres",
        user=host["SAFE_DB_USER"],
        password=Path(host["SAFE_DB_PASSWORD_FILE"]).read_text(encoding="utf-8").strip(),
        autocommit=True,
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    env = demo_env(SAFE_DB_NAME=db_name) | _support_files(work)
    legacy_file = work / "legacy-credential"
    legacy_credential = secrets.token_urlsafe(demo_seed.CREDENTIAL_BYTES)
    upto_0045 = work / "migrations-0045"
    upto_0045.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < "0046":
            (upto_0045 / path.name).write_bytes(path.read_bytes())

    with patch.dict(os.environ, env, clear=False):
        with patch.object(migrate_module, "MIGRATIONS_DIR", upto_0045):
            migrate()
        # The first seed version as it was before this change: the administrator's credential is in the marker only.
        with connect() as conn:
            demo_seed._insert_dataset(conn, hash_password(legacy_credential), storage_from_env(), key_provider_from_env())
        legacy_file.write_text(legacy_credential + "\n", encoding="utf-8")
        os.chmod(legacy_file, 0o600)
        migrate()  # the full migrations directory: 0046 copies the administrator's hash into the credential table

        assert _marker_version() == 1
        admin_hash = demo_credential_hash(DEMO_ADMIN_ID)
        assert verify_password(admin_hash, legacy_credential)

        credential_dir = work / "credentials"
        upgrade_env = dict(os.environ) | {CREDENTIAL_DIR: str(credential_dir), CREDENTIAL_FILE: str(legacy_file)}
        outcome = demo_seed.upgrade(upgrade_env)

        assert outcome.created is True
        assert outcome.applied == (2,)
        assert _marker_version() == 2
        files = _credential_files(credential_dir)
        assert set(files) == set(ROLE_USERNAMES) - {DEMO_ADMIN_USERNAME}
        for username, path in files.items():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert verify_password(_stored_hash(username), path.read_text(encoding="utf-8").strip())
        # The administrator keeps its credential and its file.
        assert demo_credential_hash(DEMO_ADMIN_ID) == admin_hash
        assert verify_password(demo_credential_hash(DEMO_ADMIN_ID), legacy_credential)
        assert legacy_file.read_text(encoding="utf-8") == legacy_credential + "\n"
        assert not (credential_dir / f"{DEMO_ADMIN_USERNAME}{demo_seed.CREDENTIAL_SUFFIX}").exists()
        with connect() as conn:
            assignments = conn.execute(
                "SELECT count(*) FROM case_assignments WHERE case_id = %s AND user_id = %s AND NOT is_deleted",
                (demo_seed.CASE_THREE, demo_seed.USER_BY_ROLE["investigator"]),
            ).fetchone()[0]
        assert assignments == 1

        counts = _table_counts()
        snapshot = _directory_snapshot(credential_dir)
        assert demo_seed.upgrade(upgrade_env).created is False
        assert demo_seed.seed(upgrade_env).created is False
        assert _table_counts() == counts
        assert _directory_snapshot(credential_dir) == snapshot


def _demo_sign_in(dataset: dict, username: str, credential: str, *, peer=LOOPBACK) -> object:
    client = TestClient(api.app, client=peer)
    with patch.dict(os.environ, dataset["env"], clear=False):
        return client.post(SIGN_IN, json={"username": username, "credential": credential})


@requires_database
def test_each_demo_role_signs_in_with_its_own_credential(demo_dataset):
    for username in ROLE_USERNAMES:
        response = _demo_sign_in(demo_dataset, username, _credential(demo_dataset, username))
        assert response.status_code == 200, username
        body = response.json()
        assert body["password_change_required"] is False
        assert body["access_token"]
        assert "safe_access" in response.cookies


@requires_database
def test_another_roles_credential_is_refused(demo_dataset):
    response = _demo_sign_in(demo_dataset, "demo-investigator", _credential(demo_dataset, "demo-triager"))
    assert response.status_code == 401
    assert "safe_access" not in response.cookies


@requires_database
def test_wrong_credential_and_non_demo_username_get_the_same_refusal(demo_dataset):
    triager = _credential(demo_dataset, "demo-triager")
    attempts = [
        ("demo-triager", triager + "x"),
        ("nobody-at-all", triager),
        ("demo-reporting-owner", triager),
        ("demo-investigator", triager),
    ]
    responses = [_demo_sign_in(demo_dataset, username, credential) for username, credential in attempts]
    assert {response.status_code for response in responses} == {401}
    assert len({json.dumps(response.json(), sort_keys=True) for response in responses}) == 1
    assert all("safe_access" not in response.cookies for response in responses)


@requires_database
def test_demo_sign_in_is_refused_for_a_remote_caller_even_with_the_right_credential(demo_dataset):
    response = _demo_sign_in(demo_dataset, "demo-reporter", _credential(demo_dataset, "demo-reporter"), peer=REMOTE)
    assert response.status_code == 403


@requires_database
def test_demo_sign_in_accepts_loopback_forwarded_by_the_trusted_gateway_only(demo_dataset):
    credential = _credential(demo_dataset, "demo-reporter")
    with patch.dict(os.environ, demo_dataset["env"] | {"SAFE_TRUSTED_PROXIES": GATEWAY}, clear=False):
        accepted = TestClient(api.app, client=(GATEWAY, 50000)).post(
            SIGN_IN,
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"username": "demo-reporter", "credential": credential},
        )
        # The same header from an address that is not a trusted proxy is ignored.
        forged = TestClient(api.app, client=REMOTE).post(
            SIGN_IN,
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"username": "demo-reporter", "credential": credential},
        )
    assert accepted.status_code == 200
    assert "safe_access" in accepted.cookies
    assert forged.status_code == 403


@requires_database
def test_production_mode_never_accepts_a_demo_credential(demo_dataset):
    client = TestClient(api.app, client=LOOPBACK)
    with process_env(production_env()):
        response = client.post(
            SIGN_IN, json={"username": "demo-admin", "credential": _credential(demo_dataset, "demo-admin")}
        )
    assert response.status_code == 403
    assert "safe_access" not in response.cookies


def _signed_in(dataset: dict, username: str) -> TestClient:
    response = _demo_sign_in(dataset, username, _credential(dataset, username))
    assert response.status_code == 200, username
    client = TestClient(api.app, client=LOOPBACK)
    client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})
    return client


# What each demo role sees under the existing rules: triager and approver read every case (cases:read_all); the
# investigator and the reviewer read the cases they are assigned to; the reporter reads only cases it created; the
# auditor and the administrator hold no case read permission, so the case list refuses them with 403.
EXPECTED_CASE_VIEW = {
    "demo-admin": None,
    "demo-reporter": set(),
    "demo-triager": set(SEEDED_CASES),
    "demo-investigator": set(SEEDED_CASES),
    "demo-reviewer": {demo_seed.CASE_ONE, demo_seed.CASE_THREE},
    "demo-approver": set(SEEDED_CASES),
    "demo-auditor": None,
}


def _listed_case_ids(client: TestClient) -> set[str]:
    """Every case the caller's case list returns, read page by page."""
    ids: set[str] = set()
    offset = 0
    while True:
        page = client.get("/api/v1/cases", params={"limit": 200, "offset": offset}).json()
        ids |= {item["id"] for item in page["items"]}
        offset += 200
        if offset >= page["total"]:
            return ids


@requires_database
def test_seeded_cases_are_visible_to_the_roles_their_real_role_allows(demo_dataset):
    with patch.dict(os.environ, demo_dataset["env"], clear=False):
        for username, expected in EXPECTED_CASE_VIEW.items():
            client = _signed_in(demo_dataset, username)
            listing = client.get("/api/v1/cases", params={"limit": 200})
            if expected is None:
                assert listing.status_code == 403, username
                for case_id in SEEDED_CASES:
                    assert client.get(f"/api/v1/cases/{case_id}").status_code == 403, username
                continue
            assert listing.status_code == 200, username
            # The database is shared with the other test modules, so only the seeded cases are compared.
            listed = _listed_case_ids(client) & set(SEEDED_CASES)
            assert listed == expected, username
            for case_id in SEEDED_CASES:
                detail = client.get(f"/api/v1/cases/{case_id}")
                assert detail.status_code == (200 if case_id in expected else 404), (username, case_id)

