"""scripts/demo.sh and scripts/demo-egress-check.sh: syntax, argument handling, and no credential in any output (F2 P13).

A fake docker, a fake curl and a fake uv are put first on PATH. The fake docker records its arguments (never its stdin),
answers the calls that scripts/demo.sh makes. The fake uv records its arguments and the variables that cli-show sets for
the CLI, and never reads a credential. No container, image, or secret file is touched, and start stops before
scripts/init-secrets.sh runs. cli-show runs the CLI on the host (uv run), so a test checks the arguments it passes.
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
  *" ps"*)
    # `ps -q` lists container IDs only; it prints one when this project's gateway runs (DEMO_STUB_GATEWAY_RUNNING=1).
    case "$*" in
      *" -q"*) if [ "${DEMO_STUB_GATEWAY_RUNNING:-}" = "1" ]; then echo "gateway-container-id"; fi; exit 0 ;;
    esac
    echo "NAME  STATE  HEALTH"; exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_CURL = "#!/bin/sh\nexit 7\n"
# The published loopback port answers, as it does when another Compose project holds it.
FAKE_CURL_ANSWERS = "#!/bin/sh\nexit 0\n"

# Test double for uv. Records its arguments and the variables cli-show sets for the CLI. Reads no credential.
FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$DEMO_UV_LOG"
printf 'SAFE_API_URL=%s\\nSSL_CERT_FILE=%s\\nPYTHONPATH=%s\\n' "$SAFE_API_URL" "$SSL_CERT_FILE" "$PYTHONPATH" >> "$DEMO_UV_LOG"
exit 0
"""


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("uv", FAKE_UV)):
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    demo_dir = tmp_path / "demo"
    credential_dir = demo_dir / "credentials"
    credential_dir.mkdir(parents=True)
    (credential_dir / "demo-investigator.credential").write_text(CREDENTIAL + "\n", encoding="utf-8")
    (credential_dir / "demo-triager.credential").write_text("triager-" + CREDENTIAL + "\n", encoding="utf-8")
    # The development CA that cli-show trusts for the host CLI. Only its path is passed around.
    ca = tmp_path / "ca.crt"
    ca.write_text("not a real certificate: the fake uv never reads it\n", encoding="utf-8")
    log = tmp_path / "docker.log"
    uv_log = tmp_path / "uv.log"
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "SAFE_DEMO_DIR": str(demo_dir),
        "SAFE_DEV_CA_FILE": str(ca),
        "DEMO_STUB_LOG": str(log),
        "DEMO_UV_LOG": str(uv_log),
    }
    return {"env": env, "demo_dir": demo_dir, "log": log, "uv_log": uv_log, "ca": ca}


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


def _port_answers(stub_env) -> None:
    # The stub curl is the first entry on PATH; rewriting it keeps its executable mode.
    stubs = Path(stub_env["env"]["PATH"].split(os.pathsep)[0])
    (stubs / "curl").write_text(FAKE_CURL_ANSWERS, encoding="utf-8")


def test_status_is_not_ready_when_another_stack_answers_the_port(stub_env):
    # The owner's stack can hold 127.0.0.1:8443 and answer the probe. Without this project's gateway, status says so.
    _port_answers(stub_env)
    result = _run(DEMO, ["status"], stub_env["env"])
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "demo: not ready at https://127.0.0.1:8443" in output
    assert "ready at" not in output.replace("not ready at", "")


def test_status_is_ready_only_with_this_projects_running_gateway(stub_env):
    _port_answers(stub_env)
    env = dict(stub_env["env"], DEMO_STUB_GATEWAY_RUNNING="1")
    result = _run(DEMO, ["status"], env)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "demo: ready at https://127.0.0.1:8443" in output
    assert "not ready" not in output


def _uv_lines(stub_env) -> list[str]:
    return stub_env["uv_log"].read_text(encoding="utf-8").splitlines() if stub_env["uv_log"].exists() else []


def test_cli_show_runs_the_cli_on_the_host_through_the_gateway_as_the_demo_investigator(stub_env):
    result = _run(DEMO, ["cli-show", CASE_ID], stub_env["env"])
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert CREDENTIAL not in output
    command, *variables = _uv_lines(stub_env)
    assert command.startswith("run --quiet --project ")
    credential_file = stub_env["demo_dir"] / "credentials" / "demo-investigator.credential"
    assert command.endswith(
        f"python -m safe_kit.cli --no-banner case show {CASE_ID} --json "
        f"--demo-username demo-investigator --demo-credential-file {credential_file}"
    )
    assert variables == [
        "SAFE_API_URL=https://127.0.0.1:8443",
        f"SSL_CERT_FILE={stub_env['ca']}",
        f"PYTHONPATH={REPO_ROOT / 'backend' / 'src'}",
    ]
    # Nothing reached the container path, and the credential value is in no argument or log.
    assert not stub_env["log"].exists()
    assert CREDENTIAL not in stub_env["uv_log"].read_text(encoding="utf-8")


def test_cli_show_uses_the_demo_user_named_in_the_environment(stub_env):
    env = dict(stub_env["env"], SAFE_DEMO_USERNAME="demo-triager")
    result = _run(DEMO, ["cli-show", CASE_ID], env)

    assert result.returncode == 0, result.stdout + result.stderr
    command = _uv_lines(stub_env)[0]
    assert "--demo-username demo-triager" in command
    assert command.endswith("credentials/demo-triager.credential")
    assert "triager-" + CREDENTIAL not in result.stdout + result.stderr


def test_cli_show_refuses_a_name_outside_the_demo_roles_before_running_the_cli(stub_env):
    env = dict(stub_env["env"], SAFE_DEMO_USERNAME="demo-reporting-owner")
    result = _run(DEMO, ["cli-show", CASE_ID], env)
    assert result.returncode == 2
    assert "must be one of the demo roles" in result.stderr
    assert _uv_lines(stub_env) == []


def test_cli_show_refuses_when_the_development_ca_is_missing(stub_env):
    env = dict(stub_env["env"], SAFE_DEV_CA_FILE=str(stub_env["demo_dir"] / "absent-ca.crt"))
    result = _run(DEMO, ["cli-show", CASE_ID], env)
    assert result.returncode == 1
    assert "development CA is missing" in result.stderr
    assert _uv_lines(stub_env) == []


def test_cli_show_refuses_a_bad_case_id_and_a_missing_credential(stub_env):
    result = _run(DEMO, ["cli-show", "not-a-uuid"], stub_env["env"])
    assert result.returncode == 2
    assert CREDENTIAL not in result.stdout + result.stderr

    (stub_env["demo_dir"] / "credentials" / "demo-investigator.credential").unlink()
    result = _run(DEMO, ["cli-show", CASE_ID], stub_env["env"])
    assert result.returncode == 1
    assert "run scripts/demo.sh start first" in result.stderr
    assert _uv_lines(stub_env) == []
