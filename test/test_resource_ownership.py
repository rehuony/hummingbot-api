"""Resource ownership checks use disposable files and mock Docker mutations."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from test.test_operations import CONFIG

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESOLVED = """name: checkout
services:
  hummingbot-api:
    image: rehuony/hummingbot-api:main
networks:
  emqx-bridge:
    name: custom-network
volumes:
  emqx-data:
    name: custom-broker-data
  postgres-data:
    name: custom-postgres-data
"""


@pytest.fixture
def resources(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(ROOT / "scripts", root / "scripts")
    for name in ("version.py", "docker-compose.yml", "docker-compose.tailscale.yml"):
        shutil.copy(ROOT / name, root / name)
    (root / ".env").write_text(CONFIG)
    configuration = tmp_path / "resolved.yml"
    configuration.write_text(RESOLVED)
    state = tmp_path / "resources.json"
    state.write_text(json.dumps({"volume": {}, "network": {}}))
    commands = tmp_path / "bin"
    commands.mkdir()
    mock = commands / "mock"
    mock.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
import sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")
if name == "git":
    if args[:2] == ["rev-parse", "--verify"]: print("a" * 40)
    elif args[0] == "symbolic-ref": print("main")
elif name == "docker":
    if args[0] == "compose" and "config" in args:
        print(Path(os.environ["RESOLVED_CONFIG"]).read_text())
    elif args[0] in ("volume", "network"):
        state = json.loads(Path(os.environ["RESOURCE_STATE"]).read_text())[args[0]]
        if args[1] == "ls":
            if os.getenv("FAIL_LIST") == args[0]: sys.exit(7)
            print("\n".join(state))
        elif args[1] == "inspect":
            if os.getenv("FAIL_INSPECT") == args[0]: sys.exit(8)
            print(state[args[-1]])
''')
    mock.chmod(0o755)
    for command in ("docker", "git"):
        (commands / command).symlink_to(mock)
    environment = {
        "PATH": f"{commands}:{os.environ['PATH']}", "HOME": str(tmp_path),
        "CALL_LOG": str(tmp_path / "calls"), "RESOLVED_CONFIG": str(configuration),
        "RESOURCE_STATE": str(state), "PYTHONDONTWRITEBYTECODE": "1",
    }
    return root, environment


def set_resources(fixture, **state):
    Path(fixture[1]["RESOURCE_STATE"]).write_text(json.dumps({"volume": {}, "network": {}, **state}))


def run(fixture, script="stack.sh", action="deploy", **extra):
    root, environment = fixture
    command = ["bash", str(root / "scripts/shell" / script)]
    if action:
        command.append(action)
    return subprocess.run(command, cwd=root.parent, env={**environment, **extra},
                          capture_output=True, text=True, timeout=20)


def calls(fixture):
    path = Path(fixture[1]["CALL_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assert_no_mutations(fixture):
    for call in calls(fixture):
        assert call[:2] != ["docker", "build"]
        assert call[:3] not in (["docker", "volume", "rm"], ["docker", "network", "rm"])
        if call[:2] == ["docker", "compose"]:
            assert not any(action in call for action in ("up", "down", "stop", "rm"))
    assert (fixture[0] / ".env").read_text() == CONFIG
    assert not (fixture[0] / ".emqx").exists()


@pytest.mark.parametrize("script,action", [
    ("stack.sh", "deploy"), ("stack.sh", "stop"), ("reset.sh", None), ("emqx.sh", "auth-reset"),
])
@pytest.mark.parametrize("kind,name,label", [
    ("volume", "custom-postgres-data", "foreign|postgres-data"),
    ("network", "custom-network", "foreign|emqx-bridge"),
])
def test_foreign_resolved_resources_block_every_lifecycle_mutation(resources, script, action, kind, name, label):
    set_resources(resources, **{kind: {name: label}})
    result = run(resources, script, action)
    assert result.returncode != 0
    assert f"resolved {kind} {name}" in result.stderr
    assert_no_mutations(resources)


@pytest.mark.parametrize("label", ["checkout|wrong-logical-resource", "<no value>|<no value>", "|"])
def test_project_label_alone_is_not_sufficient(resources, label):
    set_resources(resources, volume={"custom-postgres-data": label})
    result = run(resources)
    assert result.returncode != 0
    assert_no_mutations(resources)


@pytest.mark.parametrize("existing", [False, True])
def test_new_or_correctly_owned_resources_allow_deployment(resources, existing):
    if existing:
        set_resources(resources, volume={"custom-postgres-data": "checkout|postgres-data"},
                      network={"custom-network": "checkout|emqx-bridge"})
    result = run(resources)
    assert result.returncode == 0, result.stderr
    assert ["docker", "build"] == next(call[:2] for call in calls(resources) if call[1] == "build")
    assert any("up" in call for call in calls(resources))


@pytest.mark.parametrize("kind,name", [("volume", "custom-postgres-data"), ("network", "custom-network")])
@pytest.mark.parametrize("failure", ["FAIL_LIST", "FAIL_INSPECT"])
def test_failed_ownership_queries_block_mutations(resources, kind, name, failure):
    set_resources(resources, **{kind: {name: "checkout|unused"}})
    result = run(resources, **{failure: kind})
    assert result.returncode != 0
    assert_no_mutations(resources)


def test_external_resources_are_not_claimed_or_inspected(resources):
    configuration = Path(resources[1]["RESOLVED_CONFIG"])
    configuration.write_text(RESOLVED.replace("name: custom-network", "name: custom-network\n    external: true")
                             .replace("name: custom-postgres-data", "name: custom-postgres-data\n    external: true"))
    set_resources(resources, volume={"custom-postgres-data": "foreign|postgres-data"},
                  network={"custom-network": "foreign|emqx-bridge"})
    result = run(resources, "stack.sh", "stop")
    assert result.returncode == 0, result.stderr
    assert not any(call[1:3] in (["volume", "inspect"], ["network", "inspect"]) for call in calls(resources))
    down = next(call for call in calls(resources) if "down" in call)
    assert "-v" not in down


def test_broker_reset_refuses_external_data_before_stopping_broker(resources):
    configuration = Path(resources[1]["RESOLVED_CONFIG"])
    configuration.write_text(RESOLVED.replace("name: custom-broker-data", "name: custom-broker-data\n    external: true"))
    result = run(resources, "emqx.sh", "auth-reset")
    assert result.returncode != 0
    assert "externally managed" in result.stderr
    assert_no_mutations(resources)


@pytest.mark.parametrize("action", ["auth", "auth-reset"])
def test_directory_bootstrap_is_rejected_before_broker_changes(resources, action):
    bootstrap = resources[0] / ".emqx/auth-bootstrap.csv"
    bootstrap.mkdir(parents=True)
    result = run(resources, "emqx.sh", action)
    assert result.returncode != 0
    assert "regular file" in result.stderr
    assert list(bootstrap.iterdir()) == []
    assert not any("rm" in call or "up" in call for call in calls(resources))
    assert (resources[0] / ".env").read_text() == CONFIG


def test_broker_reset_removes_only_current_resolved_data_volume(resources):
    set_resources(resources, volume={"custom-broker-data": "checkout|emqx-data", "historical-broker-data": "checkout|emqx-data"})
    result = run(resources, "emqx.sh", "auth-reset")
    assert result.returncode == 0, result.stderr
    removals = [call for call in calls(resources) if call[1:3] == ["volume", "rm"]]
    assert removals == [["docker", "volume", "rm", "custom-broker-data"]]


def test_resource_labels_follow_resolved_custom_project(resources):
    configuration = Path(resources[1]["RESOLVED_CONFIG"])
    configuration.write_text(RESOLVED.replace("name: checkout", "name: private-project"))
    set_resources(resources, volume={"custom-broker-data": "private-project|emqx-data"},
                  network={"custom-network": "private-project|emqx-bridge"})
    result = run(resources, "emqx.sh", "auth-reset")
    assert result.returncode == 0, result.stderr
    scoped_containers = next(call for call in calls(resources) if call[:3] == ["docker", "ps", "-aq"])
    assert "label=com.docker.compose.project=private-project" in scoped_containers
    assert ["docker", "volume", "rm", "custom-broker-data"] in calls(resources)


def test_reset_checks_local_symlinks_before_docker_teardown(resources):
    (resources[0] / "bots").symlink_to(resources[0].parent, target_is_directory=True)
    result = run(resources, "reset.sh", None)
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert calls(resources) == []


def test_canonical_resource_fields_are_required(resources):
    Path(resources[1]["RESOLVED_CONFIG"]).write_text(RESOLVED.replace("    name: custom-postgres-data\n", ""))
    result = run(resources)
    assert result.returncode != 0
    assert "resource identity" in result.stderr
    assert_no_mutations(resources)


@pytest.mark.skipif(not shutil.which("docker"), reason="requires Docker Compose CLI, no daemon or mutations")
def test_resource_identity_from_actual_offline_compose_output(resources):
    root, environment = resources
    override = root / "docker-compose.override.yml"
    override.write_text("""volumes:
  postgres-data:
    name: actual-overridden-database
networks:
  emqx-bridge:
    name: actual-external-network
    external: true
""")
    result = subprocess.run([
        shutil.which("docker"), "compose", "--project-directory", str(root), "--env-file", str(root / ".env"),
        "-f", str(root / "docker-compose.yml"), "-f", str(override), "config",
    ], cwd=root.parent, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    Path(environment["RESOLVED_CONFIG"]).write_text(result.stdout)
    set_resources(resources, volume={"actual-overridden-database": "foreign|postgres-data"},
                  network={"actual-external-network": "foreign|emqx-bridge"})
    result = run(resources, "reset.sh", None)
    assert result.returncode != 0
    assert "actual-overridden-database" in result.stderr
    assert_no_mutations(resources)
