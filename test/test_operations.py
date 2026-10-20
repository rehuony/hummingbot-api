"""Exercise operational scripts using disposable checkouts and mock commands only."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = """USERNAME=fixture-user
PASSWORD=FixtureApiPassword1
CONFIG_PASSWORD=FixtureEncryptionPassword2
BROKER_USERNAME=fixture-broker
BROKER_PASSWORD=FixtureBrokerPassword3
BROKER_DASHBOARD_PASSWORD=FixtureDashboardPassword4
TAILSCALE_ENABLED=false
TAILSCALE_MODE=none
"""


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(ROOT / "scripts", root / "scripts")
    for name in ("Makefile", "version.py", "docker-compose.yml", "docker-compose.tailscale.yml"):
        shutil.copy(ROOT / name, root / name)
    (root / ".env").write_text(CONFIG)
    commands = tmp_path / "bin"
    commands.mkdir()
    mock = commands / "mock"
    mock.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
import subprocess
import sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as stream:
    stream.write(json.dumps([name, *args]) + '\n')
if name == 'git':
    if args[:2] == ['rev-parse', '--verify']: print('a' * 40)
    elif args[0] == 'symbolic-ref': print('main')
elif name == 'docker':
    if args[0] == 'info': sys.exit(int(os.getenv('DOCKER_FAILURE', '0')))
    if args[0] == 'build': sys.exit(int(os.getenv('BUILD_FAILURE', '0')))
    if args[0] == 'compose':
        if 'config' in args:
            if '--no-env-resolution' in args:
                image = os.getenv('OVERRIDE_IMAGE', os.getenv('API_IMAGE', 'rehuony/hummingbot-api:main'))
                print('services:\n  hummingbot-api:\n    image: ' + image)
            else:
                project = os.getenv('COMPOSE_PROJECT_NAME', 'checkout')
                print('name: ' + project + '\nvolumes:\n  emqx-data:\n    name: ' + project + '_emqx-data')
        if 'up' in args: sys.exit(int(os.getenv('UP_FAILURE', '0')))
        if 'down' in args: sys.exit(int(os.getenv('DOWN_FAILURE', '0')))
    elif args[0] == 'ps':
        if os.getenv('FOREIGN_CONTAINER'):
            print('hummingbot-broker')
    elif args[0] == 'inspect':
        print('foreign' if 'project"' in args[2] else '/foreign/checkout')
    elif args[:2] == ['volume', 'ls']:
        if os.getenv('VOLUME_QUERY_FAILURE'): sys.exit(9)
        if os.getenv('LEFTOVER_VOLUME'): print('checkout_emqx-data')
elif name == 'conda':
    if args[:2] == ['env', 'list']: print('hummingbot-api /fixture/env')
    if 'python' in args:
        sys.exit(subprocess.call([sys.executable, *args[args.index('python') + 1:]]))
    if 'uvicorn' in args:
        Path(os.environ['CREDENTIAL_CHECK']).write_text(
            str(os.getenv('BROKER_PASSWORD') == 'FixtureBrokerPassword3'))
        if os.getenv('HOLD_DEV_SESSION'):
            import time
            time.sleep(30)
elif name == 'curl':
    print('000')
''')
    mock.chmod(0o755)
    for name in ("docker", "git", "conda", "curl", "sudo", "tailscale", "lsof", "ss", "pgrep", "ip", "ifconfig"):
        (commands / name).symlink_to(mock)
    environment = {
        "PATH": f"{commands}:{os.environ['PATH']}", "HOME": str(tmp_path),
        "CALL_LOG": str(tmp_path / "calls"), "CREDENTIAL_CHECK": str(tmp_path / "credential-check"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return root, environment


def run(checkout, script="stack.sh", *args, **extra):
    root, environment = checkout
    return subprocess.run(
        ["bash", str(root / "scripts/shell" / script), *args], cwd=root.parent,
        env={**environment, **extra}, capture_output=True, text=True, timeout=30,
    )


def calls(checkout):
    path = Path(checkout[1]["CALL_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def env_value(checkout, key, *, compose=False, **extra):
    root, environment = checkout
    return subprocess.run(
        ["bash", "-c", '. "$1"; ' + ("config_get" if compose else "env_get") + ' "$2"',
         "read-config", str(root / "scripts/shell/lib/common.sh"), key],
        cwd=root.parent, env={**environment, **extra}, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("value,expected", [
    ("first\nVALUE=last", "last"), ("'quoted # value'", "quoted # value"),
    ('"double quoted" # comment', "double quoted"), ("abc#def=ghi", "abc#def=ghi"),
    ("literal ; & | ( )", "literal ; & | ( )"), ("plain # comment", "plain"), ("", ""),
])
def test_env_literals_and_duplicate_keys(checkout, value, expected):
    (checkout[0] / ".env").write_text(f"# comment\r\nexport VALUE = {value}\r\n")
    result = env_value(checkout, "VALUE")
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert calls(checkout) == []


@pytest.mark.parametrize("value", ['$(touch PWNED)', '`touch PWNED`', '"first\nVALUE=spoof\nlast"', r'"escaped\nvalue"'])
def test_unsupported_env_syntax_never_executes_or_leaks(checkout, value):
    (checkout[0] / ".env").write_text(f"VALUE={value}\n")
    result = env_value(checkout, "VALUE")
    assert result.returncode != 0
    assert "line" in result.stderr
    assert value not in result.stdout + result.stderr
    assert not (checkout[0] / "PWNED").exists()


def test_unknown_multiline_cannot_spoof_operational_assignment(checkout):
    (checkout[0] / ".env").write_text('OTHER="first\nBROKER_PASSWORD=spoof\nlast"\n')
    result = env_value(checkout, "BROKER_PASSWORD")
    assert result.returncode != 0
    assert "spoof" not in result.stdout + result.stderr


def test_process_precedence_is_limited_to_compose_controls(checkout):
    (checkout[0] / ".env").write_text("DB_PORT=15432\nBROKER_PASSWORD=file-secret\n")
    assert env_value(checkout, "DB_PORT", compose=True, DB_PORT="25432").stdout == "25432"
    assert env_value(checkout, "BROKER_PASSWORD", BROKER_PASSWORD="shell-secret").stdout == "file-secret"


def test_deploy_always_builds_before_bootstrap_and_service_changes(checkout):
    for _ in range(2):
        result = run(checkout, "stack.sh", "deploy", API_IMAGE="example/api:local")
        assert result.returncode == 0, result.stderr
    log = calls(checkout)
    builds = [i for i, call in enumerate(log) if call[:2] == ["docker", "build"]]
    starts = [i for i, call in enumerate(log) if call[:2] == ["docker", "compose"] and "up" in call]
    assert len(builds) == len(starts) == 2
    assert builds[0] < starts[0] < builds[1] < starts[1]
    assert all("example/api:local" in log[i] for i in builds)
    assert (checkout[0] / ".emqx/auth-bootstrap.csv").is_file()


def test_failed_build_preserves_bootstrap_and_never_changes_services(checkout):
    directory = checkout[0] / ".emqx"
    directory.mkdir()
    bootstrap = directory / "auth-bootstrap.csv"
    bootstrap.write_text("previous-bootstrap")
    result = run(checkout, "stack.sh", "deploy", BUILD_FAILURE="7")
    assert result.returncode != 0
    assert bootstrap.read_text() == "previous-bootstrap"
    assert not any(any(action in call for action in ("up", "stop", "down")) for call in calls(checkout))


def test_deploy_failure_is_not_hidden_by_host_tailscale_success(checkout):
    result = run(checkout, "stack.sh", "deploy", TAILSCALE_MODE="host", UP_FAILURE="9")
    assert result.returncode != 0
    assert not any("serve" in call for call in calls(checkout))


def test_missing_env_is_not_hidden_by_setup_sentinel(checkout):
    (checkout[0] / ".env").unlink()
    (checkout[0] / ".setup-complete").touch()
    result = run(checkout, "stack.sh", "deploy")
    assert result.returncode != 0
    assert "make setup" in result.stderr
    assert calls(checkout) == []


def test_stop_includes_sidecar_after_mode_change_without_build_or_volume_removal(checkout):
    result = run(checkout, "stack.sh", "stop")
    assert result.returncode == 0, result.stderr
    down = next(call for call in calls(checkout) if "down" in call)
    assert str(checkout[0] / "docker-compose.tailscale.yml") in down
    assert "-v" not in down
    assert not any(call[:2] == ["docker", "build"] for call in calls(checkout))


@pytest.mark.parametrize("action", ["deploy", "stop"])
def test_foreign_container_refuses_all_mutations(checkout, action):
    result = run(checkout, "stack.sh", action, FOREIGN_CONTAINER="1")
    assert result.returncode != 0
    assert "another project or checkout" in result.stderr
    assert not any(any(action in call for action in ("build", "up", "stop", "down")) for call in calls(checkout))


@pytest.mark.parametrize("port", ["0", "65536", "bad", "-1"])
def test_invalid_host_ports_fail_before_docker(checkout, port):
    result = run(checkout, "stack.sh", "deploy", DB_PORT=port)
    assert result.returncode != 0
    assert "DB_PORT" in result.stderr
    assert calls(checkout) == []


def test_custom_project_name_drives_volume_scope(checkout):
    result = run(checkout, "emqx.sh", "auth-reset", COMPOSE_PROJECT_NAME="private-test")
    assert result.returncode == 0, result.stderr
    # The resolved resource name is used; do not delete every historical volume
    # that happens to carry this project's logical emqx-data label.
    scoped_containers = next(call for call in calls(checkout) if call[:3] == ["docker", "ps", "-aq"])
    assert "label=com.docker.compose.project=private-test" in scoped_containers
    assert any(call[:3] == ["docker", "volume", "ls"] for call in calls(checkout))
    assert not any("--filter" in call for call in calls(checkout) if call[:3] == ["docker", "volume", "ls"])


@pytest.mark.parametrize("failure", ["DOCKER_FAILURE", "DOWN_FAILURE", "VOLUME_QUERY_FAILURE", "LEFTOVER_VOLUME"])
def test_reset_failures_preserve_configuration_and_credentials(checkout, failure):
    profile = checkout[0] / "bots/credentials/master_account/private.yml"
    profile.parent.mkdir(parents=True)
    profile.write_text("keep-private-data")
    before = (checkout[0] / ".env").read_bytes()
    result = run(checkout, "reset.sh", **{failure: "1"})
    assert result.returncode != 0
    assert (checkout[0] / ".env").read_bytes() == before
    assert profile.read_text() == "keep-private-data"
    assert not any(call[0] in {"pgrep", "pkill"} for call in calls(checkout))


def test_reset_refuses_active_checkout_dev_session(checkout):
    session = checkout[0] / ".dev-session"
    session.mkdir()
    (session / "pid").write_text(str(os.getpid()))
    result = run(checkout, "reset.sh")
    assert result.returncode != 0
    assert "development session" in result.stderr
    assert (checkout[0] / ".env").exists()
    assert calls(checkout) == []


def test_auth_preserves_file_credentials_and_permissions(checkout):
    result = run(checkout, "emqx.sh", "auth", BROKER_PASSWORD="shell-secret")
    assert result.returncode == 0, result.stderr
    bootstrap = checkout[0] / ".emqx/auth-bootstrap.csv"
    assert "FixtureBrokerPassword3,false" in bootstrap.read_text()
    assert "FixtureBrokerPassword3" not in result.stdout + result.stderr
    assert bootstrap.stat().st_mode & 0o777 == 0o644
    assert bootstrap.parent.stat().st_mode & 0o777 == 0o700
    assert calls(checkout) == []


def test_native_run_rejects_mismatch_before_starting_dependencies(checkout):
    result = run(checkout, "dev.sh", "run", BROKER_PUBLISHED_PORT="2883")
    assert result.returncode != 0
    assert "host-run" in result.stderr
    assert not any("up" in call or "stop" in call for call in calls(checkout))
    assert not (checkout[0] / ".dev-session").exists()


def test_native_run_uses_file_broker_credentials_and_cleans_session(checkout):
    result = run(checkout, "dev.sh", "run", BROKER_PASSWORD="shell-secret")
    assert result.returncode == 0, result.stderr
    assert Path(checkout[1]["CREDENTIAL_CHECK"]).read_text() == "True"
    assert not (checkout[0] / ".dev-session").exists()
    assert not any("docker-compose.source.yml" in str(call) for call in calls(checkout))


def test_doctor_still_checks_configuration_without_docker_daemon(checkout):
    result = run(checkout, "doctor.sh", DOCKER_FAILURE="1")
    assert result.returncode != 0
    assert "Configuration" in result.stdout
    assert "USERNAME" in result.stdout
    assert "FixtureBrokerPassword3" not in result.stdout + result.stderr
    assert not any("up" in call or "build" in call or "down" in call for call in calls(checkout))


@pytest.mark.skipif(not shutil.which("make"), reason="requires Make on the host")
def test_make_respects_env_file_image_override_when_no_cli_value(checkout):
    root, environment = checkout
    with (root / ".env").open("a") as stream:
        stream.write("API_IMAGE=example/api:env-file\nBOT_IMAGE=example/bot:env-file\n")
    for target in ("build", "image-info", "pull-bot-image"):
        result = subprocess.run(["make", target], cwd=root, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    log = calls(checkout)
    build = next(call for call in log if call[:2] == ["docker", "build"])
    assert build[build.index("-t") + 1] == "example/api:env-file"
    assert ["docker", "image", "inspect", "example/api:env-file"] == next(
        call[:4] for call in log if call[:3] == ["docker", "image", "inspect"]
    )
    assert ["docker", "pull", "example/bot:env-file"] in log


def test_host_tailnet_direct_binding_does_not_proxy_to_absent_loopback(checkout):
    result = run(checkout, "stack.sh", "deploy", TAILSCALE_MODE="host", API_BIND="100.64.0.1")
    assert result.returncode == 0, result.stderr
    assert not any("serve" in call for call in calls(checkout))


def test_conflicting_compose_image_refuses_build_and_deployment(checkout):
    result = run(checkout, "stack.sh", "deploy", OVERRIDE_IMAGE="old/api:main")
    assert result.returncode != 0
    assert "image differs" in result.stderr
    assert not any("build" in call or "up" in call for call in calls(checkout))


@pytest.mark.parametrize("extra", [
    {"BROKER_HOST": "::1"},
    {"BROKER_HOST": "127.0.0.1", "BROKER_BIND": "[::1]"},
    {"DATABASE_URL": "postgresql://user:private@localhost:0/db"},
])
def test_native_run_rejects_family_mismatch_and_zero_connection_port(checkout, extra):
    result = run(checkout, "dev.sh", "run", **extra)
    assert result.returncode != 0
    assert not any("up" in call for call in calls(checkout))
    assert "private" not in result.stdout + result.stderr


def test_native_run_accepts_bracketed_ipv6_binding(checkout):
    result = run(checkout, "dev.sh", "run", BROKER_BIND="[::1]", BROKER_HOST="::1",
                 DB_BIND="[::1]", DATABASE_URL="postgresql://user:private@[::1]:5432/db")
    assert result.returncode == 0, result.stderr


def test_doctor_uses_authoritative_tailnet_mode(checkout):
    result = run(checkout, "doctor.sh", TAILSCALE_MODE="host", TAILSCALE_ENABLED="false")
    assert "host — served on this machine" in result.stdout


def test_doctor_never_sends_credentials_to_nonlocal_bind(checkout):
    result = run(checkout, "doctor.sh", API_BIND="example.invalid")
    assert "no credentials sent" in result.stdout
    assert not any(call[0] == "curl" for call in calls(checkout))


def test_doctor_normalizes_ipv6_address_once(checkout):
    run(checkout, "doctor.sh", API_BIND="[::1]")
    curl = next(call for call in calls(checkout) if call[0] == "curl")
    assert "http://[::1]:8000/system/resources" in curl


def test_database_repair_help_resolves_checkout_without_running_repair(checkout):
    root, environment = checkout
    (root / "config.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "assert Path.cwd() == Path(__file__).resolve().parent\n"
        "assert os.environ['BROKER_PASSWORD'] == 'FixtureBrokerPassword3'\n"
        "settings = object()\n"
    )
    database = root / "database"
    (database / "repositories").mkdir(parents=True)
    (database / "__init__.py").touch()
    (database / "repositories/__init__.py").touch()
    (database / "connection.py").write_text(
        "class AsyncDatabaseManager:\n    def __init__(self, *args):\n"
        "        raise AssertionError('database must not be opened')\n"
    )
    (database / "repositories/gateway_clmm_repository.py").write_text("GatewayCLMMRepository = object\n")
    result = subprocess.run(
        [sys.executable, str(root / "scripts/python/backfill_gas_tokens.py"), "--help"],
        cwd=root.parent, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout


def test_native_session_guards_reset_and_terminates_only_owned_child(checkout):
    root, environment = checkout
    process = subprocess.Popen(
        ["bash", str(root / "scripts/shell/dev.sh"), "run"], cwd=root.parent,
        env={**environment, "HOLD_DEV_SESSION": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        marker = root / ".dev-session/pid"
        while time.monotonic() < deadline:
            if (marker.parent / "pgid").exists():
                break
            if process.poll() is not None:
                pytest.fail(str(process.communicate()))
            time.sleep(0.02)
        else:
            pytest.fail("development child did not start")
        result = run(checkout, "reset.sh")
        assert result.returncode != 0
        assert "development session" in result.stderr
        process.terminate()
        process.communicate(timeout=10)
        assert process.returncode == 143
        assert not marker.parent.exists()
        assert (root / ".env").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


def test_doctor_runs_without_docker_cli(checkout):
    root, environment = checkout
    commands = Path(environment["CALL_LOG"]).parent / "bin"
    (commands / "docker").unlink()
    result = run(checkout, "doctor.sh", PATH=f"{commands}:/usr/bin:/bin")
    assert result.returncode != 0
    assert "not found" in result.stdout
    assert "Configuration" in result.stdout
    assert "FixtureApiPassword1" not in result.stdout + result.stderr
