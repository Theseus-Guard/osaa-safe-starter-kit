"""Demo egress: the application and worker networks have no route out, and only the gateway touches the edge (F2 P13).

The static tests read compose.yaml with PyYAML. The compose-config tests run `docker compose config --format json`
(read-only: no container is created) on the base file and on the demo overlay, and are skipped with a reason when the
Docker Compose plugin is not available. Both sets apply the same rules, in check_isolation below:

- backend is internal, so containers on it have no route to the host or to other networks;
- edge is not internal and is attached to the gateway only;
- the demo application (api, migrate, worker, cli) and the database services are attached to backend only;
- every published port is bound to 127.0.0.1, and the gateway publishes exactly one port (8443);
- no service uses the host network namespace.

The runtime check (DNS, IPv4 and IPv6 refused from inside the API container) is scripts/demo-egress-check.sh; it needs a
running stack and is not run by the test suite.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "compose.yaml"
DEMO_OVERLAY = REPO_ROOT / "compose.demo.yaml"
LOOPBACK = "127.0.0.1"
GATEWAY_PORT = "127.0.0.1:8443:8443"
# Services that must sit on backend and nowhere else: the application, its migrations and CLI, and the database.
BACKEND_ONLY = ("api", "worker", "migrate", "cli", "db", "db-roles")


def _networks_of(service: dict) -> set[str]:
    networks = service.get("networks") or []
    return set(networks.keys()) if isinstance(networks, dict) else set(networks)


def _published_of(service: dict) -> list[str]:
    """Each published port as 'host_ip:published:target' (host_ip is empty when the port is not bound to an address)."""
    published = []
    for entry in service.get("ports") or []:
        if isinstance(entry, dict):
            published.append(f"{entry.get('host_ip', '')}:{entry.get('published', '')}:{entry.get('target', '')}")
        else:
            parts = str(entry).split(":")
            if len(parts) == 2:
                parts.insert(0, "")
            published.append(":".join(parts))
    return published


def check_isolation(compose: dict) -> list[str]:
    """Every rule the demo needs, as a list of violations. An empty list means the demo network is isolated."""
    problems = []
    networks = compose.get("networks") or {}
    backend = networks.get("backend") or {}
    edge = networks.get("edge") or {}
    if backend.get("internal") is not True:
        problems.append("backend must be internal: true")
    if edge.get("internal"):
        problems.append("edge must not be internal, or the gateway has no host path")

    services = compose.get("services") or {}
    edge_members = {name for name, service in services.items() if "edge" in _networks_of(service)}
    if edge_members != {"gateway"}:
        problems.append(f"only the gateway may be on edge, found {sorted(edge_members)}")

    for name in BACKEND_ONLY:
        service = services.get(name)
        if service is None:
            problems.append(f"service {name} is missing")
        elif _networks_of(service) != {"backend"}:
            problems.append(f"{name} must be on backend only, found {sorted(_networks_of(service))}")

    gateway = services.get("gateway") or {}
    if _networks_of(gateway) != {"edge", "backend"}:
        problems.append("the gateway must be on edge and backend")

    for name, service in services.items():
        if "network_mode" in service:
            problems.append(f"{name} must not set network_mode")
        for entry in _published_of(service):
            host_ip = entry.split(":")[0]
            if host_ip != LOOPBACK:
                problems.append(f"{name} publishes {entry!r}; every published port must bind {LOOPBACK}")
    gateway_ports = _published_of(gateway)
    if gateway_ports != [f"{LOOPBACK}:8443:8443"]:
        problems.append(f"the gateway must publish exactly {GATEWAY_PORT!r}, found {gateway_ports}")
    return problems


def test_check_isolation_accepts_the_repository_compose_file_and_rejects_a_leak():
    # The rules are only useful if they can fail, so a broken copy of the file must be reported.
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert check_isolation(compose) == []

    leaky = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    leaky["services"]["api"]["networks"] = ["backend", "edge"]
    leaky["services"]["worker"]["ports"] = ["8080:8080"]
    leaky["networks"]["backend"]["internal"] = False
    problems = check_isolation(leaky)
    assert any("api must be on backend only" in problem for problem in problems), problems
    assert any("worker publishes" in problem for problem in problems), problems
    assert any("backend must be internal" in problem for problem in problems), problems


def test_compose_file_keeps_the_demo_network_isolated():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    assert check_isolation(compose) == []


def test_demo_overlay_adds_no_network_or_port():
    # The overlay only renames images and mounts the credential directory; it must not attach or publish anything.
    overlay = yaml.safe_load(DEMO_OVERLAY.read_text(encoding="utf-8"))
    for name, service in overlay["services"].items():
        assert "networks" not in service, name
        assert "ports" not in service, name
        assert "network_mode" not in service, name
    assert "networks" not in overlay


def _compose_unavailable_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker is not installed, so the docker compose config check cannot run"
    try:
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"docker compose could not be started: {error}"
    if probe.returncode != 0:
        return "the docker compose plugin is not available: " + (probe.stderr.strip().splitlines() or ["no output"])[0]
    return None


@pytest.mark.parametrize("with_overlay", [False, True], ids=["base", "demo-overlay"])
def test_docker_compose_config_keeps_the_demo_network_isolated(with_overlay):
    reason = _compose_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    # --profile cli renders the one-off CLI service too, so every service is checked.
    command = ["docker", "compose", "-p", "safe-egress-check", "--profile", "cli", "-f", str(COMPOSE)]
    env = dict(os.environ)
    if with_overlay:
        command += ["-f", str(DEMO_OVERLAY)]
        # The overlay requires the host credential directory; the config is only rendered, nothing is created.
        env["SAFE_DEMO_CREDENTIAL_DIR"] = tempfile.gettempdir()
    command += ["config", "--format", "json"]
    result = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)

    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert check_isolation(rendered) == []
