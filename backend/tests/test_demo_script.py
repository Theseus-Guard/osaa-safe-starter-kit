"""scripts/demo.sh and scripts/demo-egress-check.sh: syntax, argument handling, and no credential in any output (F2 P13).

A fake docker and a fake curl are put first on PATH. The fake docker records its arguments (never its stdin), answers
the calls that scripts/demo.sh makes, and saves the stdin of a `compose run` so that a test can see that the credential
reaches the CLI. No container, image, or secret file is touched, and start stops before scripts/init-secrets.sh runs.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "scripts" / "demo.sh"
EGRESS = REPO_ROOT / "scripts" / "demo-egress-check.sh"
CASE_ID = "5afe0d00-0000-4000-8000-000000000201"
CREDENTIAL = "sentinel-demo-credential-7f3a91c2"

FAKE_DOCKER = """#!/bin/sh
# Test double for docker. Records its arguments; saves the stdin of `compose run`.
printf '%s\\n' "$*" >> "$DEMO_STUB_LOG"
case "$*" in
  "info")
    if [ "${DEMO_STUB_DOCKER_DOWN:-}" = "1" ]; then exit 1; fi
    exit 0 ;;
  "compose version") echo "Docker Compose version test"; exit 0 ;;
  *" run "*) cat > "$DEMO_STUB_LOG.stdin"; echo '{"id": "stub"}'; exit 0 ;;
  *" ps"*) echo "NAME  STATE  HEALTH"; exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_CURL = "#!/bin/sh\nexit 7\n"


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    demo_dir = tmp_path / "demo"
    credential_dir = demo_dir / "credentials"
    credential_dir.mkdir(parents=True)
    (credential_dir / "demo-investigator.credential").write_text(CREDENTIAL + "\n", encoding="utf-8")
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "SAFE_DEMO_DIR": str(demo_dir),
        "DEMO_STUB_LOG": str(log),
    }
    return {"env": env, "demo_dir": demo_dir, "log": log}


def _run(script: Path, args, env, check=False):
    return subprocess.run(
        ["sh", str(script), *args], env=env, capture_output=True, text=True, timeout=60, check=check
    )


def test_both_scripts_pass_the_posix_syntax_check():
    for script in (DEMO, EGRESS):
        result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, (script.name, result.stderr)


def test_unknown_or_missing_command_exits_2():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SAFE_DEMO_DIR": "/tmp/unused"}
    assert _run(DEMO, ["bogus"], env).returncode == 2
    assert _run(DEMO, [], env).returncode == 2
    assert _run(DEMO, ["status", "extra"], env).returncode == 2


@pytest.mark.parametrize("args", [["start"], ["status"], ["cli-show", CASE_ID]])
def test_missing_demo_dir_is_refused(args):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = _run(DEMO, args, env)
    assert result.returncode == 2
    assert "SAFE_DEMO_DIR is required" in result.stderr


def test_demo_dir_must_be_absolute_and_outside_the_repository(stub_env):
    env = dict(stub_env["env"], SAFE_DEMO_DIR="relative/dir")
    result = _run(DEMO, ["status"], env)
    assert result.returncode == 2 and "absolute path" in result.stderr

    env = dict(stub_env["env"], SAFE_DEMO_DIR=str(REPO_ROOT / "demo-files"))
    result = _run(DEMO, ["status"], env)
    assert result.returncode == 2 and "outside this repository" in result.stderr


@pytest.mark.parametrize("timeout", ["0", "abc", "-5"])
def test_start_rejects_a_bad_timeout(stub_env, timeout):
    result = _run(DEMO, ["start", "--timeout", timeout], stub_env["env"])
    assert result.returncode == 2
    assert "--timeout" in result.stderr


def test_start_stops_when_docker_is_not_running(stub_env):
    env = dict(stub_env["env"], DEMO_STUB_DOCKER_DOWN="1")
    result = _run(DEMO, ["start"], env)
    assert result.returncode == 1
    assert "docker is not running" in result.stderr
    # Nothing was started: the only docker call was the daemon check.
    assert stub_env["log"].read_text(encoding="utf-8").splitlines() == ["info"]


def test_status_reports_files_without_printing_a_credential(stub_env):
    result = _run(DEMO, ["status"], stub_env["env"])
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "demo-investigator credential file present" in output
    assert "demo-admin credential file missing" in output
    assert "not ready" in output
    assert CREDENTIAL not in output
    assert CREDENTIAL not in stub_env["log"].read_text(encoding="utf-8")


def test_cli_show_passes_the_credential_on_stdin_and_never_prints_it(stub_env):
    result = _run(DEMO, ["cli-show", CASE_ID], stub_env["env"])
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert CREDENTIAL not in output
    argv = stub_env["log"].read_text(encoding="utf-8")
    assert CREDENTIAL not in argv
    assert "--json" in argv and CASE_ID in argv and "--username demo-investigator" in argv
    assert "--no-deps cli --no-banner case show" in argv
    assert Path(str(stub_env["log"]) + ".stdin").read_text(encoding="utf-8") == CREDENTIAL + "\n"


def test_cli_show_refuses_a_bad_case_id_and_a_missing_credential(stub_env):
    result = _run(DEMO, ["cli-show", "not-a-uuid"], stub_env["env"])
    assert result.returncode == 2
    assert CREDENTIAL not in result.stdout + result.stderr

    (stub_env["demo_dir"] / "credentials" / "demo-investigator.credential").unlink()
    result = _run(DEMO, ["cli-show", CASE_ID], stub_env["env"])
    assert result.returncode == 1
    assert "run scripts/demo.sh start first" in result.stderr
