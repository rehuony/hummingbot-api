"""Development supervision uses real isolated processes, never application services."""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="development scripts require POSIX process groups")


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("isolated development process did not reach the expected state")


def process_active(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    shutil.copytree(ROOT / "scripts", root / "scripts")
    (root / ".dev-session").mkdir(mode=0o700)
    (root / ".env").write_text("".join(
        f"{key}=fixture\n" for key in
        ("USERNAME", "PASSWORD", "CONFIG_PASSWORD", "BROKER_USERNAME", "BROKER_PASSWORD", "BROKER_DASHBOARD_PASSWORD")
    ))
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\n')
    docker.chmod(0o755)
    environment = {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "DOCKER_CALLS": str(tmp_path / "docker-calls"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (root / "tree.py").write_text(
        """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
role, mode = sys.argv[1:]
if mode == 'ignore':
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, lambda *_: None)
    signal.signal(signal.SIGTERM, lambda *_: None)
Path(role + '.pid').write_text(str(os.getpid()))
if role != 'worker':
    child = subprocess.Popen([sys.executable, __file__, 'reload' if role == 'wrapper' else 'worker', mode])
    if role == 'wrapper' and mode == 'orphan':
        while not Path('worker.pid').exists():
            time.sleep(.01)
        raise SystemExit(7)
    raise SystemExit(child.wait())
if mode != 'ignore':
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(.05)
"""
    )
    processes = []

    def launch(command=None, mode="normal"):
        process = subprocess.Popen(
            [sys.executable, str(root / "scripts/python/dev_session.py"),
             *(command or [sys.executable, str(root / "tree.py"), "wrapper", mode])],
            cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        processes.append(process)
        return process

    yield root, environment, launch
    # Cleanup only process groups created by this fixture, including failed assertions.
    pgid_file = root / ".dev-session/pgid"
    if pgid_file.exists():
        try:
            os.killpg(int(pgid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_stops_wrapper_reload_and_worker_but_preserves_other_processes(checkout, signum):
    root, _, launch = checkout
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        process = launch()
        wait_until(lambda: (root / "worker.pid").exists())
        pids = [int((root / f"{role}.pid").read_text()) for role in ("wrapper", "reload", "worker")]
        assert (root / ".dev-session/pid").read_text().strip() == str(process.pid)
        assert (root / ".dev-session/pgid").read_text().strip() == str(pids[0])
        assert all(os.getpgid(pid) == pids[0] for pid in pids)
        process.send_signal(signum)
        process.communicate(timeout=10)
        assert process.returncode == 128 + signum
        assert not any(process_active(pid) for pid in pids)
        assert not (root / ".dev-session").exists()
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_ignoring_workers_are_killed_before_guard_is_removed(checkout):
    root, _, launch = checkout
    process = launch(mode="ignore")
    wait_until(lambda: (root / "worker.pid").exists())
    pids = [int((root / f"{role}.pid").read_text()) for role in ("wrapper", "reload", "worker")]
    process.terminate()
    process.communicate(timeout=10)
    assert process.returncode == 143
    assert not any(process_active(pid) for pid in pids)
    assert not (root / ".dev-session").exists()


def test_exited_wrapper_does_not_leave_its_workers_serving(checkout):
    root, _, launch = checkout
    process = launch(mode="orphan")
    process.communicate(timeout=10)
    assert process.returncode == 7
    assert not any(process_active(int(path.read_text())) for path in root.glob("*.pid"))
    assert not (root / ".dev-session").exists()


@pytest.mark.parametrize("missing_group_marker", [False, True])
def test_killed_supervisor_retains_group_guard_and_blocks_reset_and_deploy(checkout, missing_group_marker):
    root, environment, launch = checkout
    process = launch()
    wait_until(lambda: (root / "worker.pid").exists())
    process.kill()
    process.wait(timeout=5)
    group_marker = root / ".dev-session/pgid"
    pgid = group_marker.read_text()
    if missing_group_marker:
        group_marker.unlink()  # Model SIGKILL between spawning and recording the group.
    before = (root / ".env").read_bytes()
    try:
        for script, arguments in (("reset.sh", []), ("stack.sh", ["deploy"])):
            result = subprocess.run(
                ["bash", str(root / "scripts/shell" / script), *arguments],
                cwd=root.parent, env=environment, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode != 0
            assert "development session" in result.stderr
        assert (root / ".env").read_bytes() == before
        assert process_active(int((root / "worker.pid").read_text()))
        assert (root / ".dev-session/pid").exists()
        assert Path(environment["DOCKER_CALLS"]).read_text().splitlines() == ["compose version", "info"]
    finally:
        group_marker.write_text(pgid)  # Let fixture cleanup stop only its own process group.


def test_startup_error_clears_guard_without_leaving_a_child(checkout):
    root, _, launch = checkout
    process = launch([str(root / "missing-command")])
    _, error = process.communicate(timeout=5)
    assert process.returncode == 1
    assert "Could not start" in error
    assert not (root / ".dev-session").exists()


@pytest.mark.skipif(importlib.util.find_spec("uvicorn") is None, reason="requires the project's uvicorn dependency")
def test_real_uvicorn_reload_worker_terminates_with_supervisor(checkout):
    root, _, launch = checkout
    # Port zero is an isolated ephemeral listener; no project app or database is imported.
    (root / "fixture_app.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('asgi-worker.pid').write_text(str(os.getpid()))\n"
        "async def app(scope, receive, send):\n"
        "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        "    await send({'type': 'http.response.body', 'body': b'test'})\n"
    )
    process = launch([
        sys.executable, "-m", "uvicorn", "fixture_app:app",
        "--reload", "--host", "127.0.0.1", "--port", "0", "--lifespan", "off",
    ])
    wait_until(lambda: (root / "asgi-worker.pid").exists())
    worker = int((root / "asgi-worker.pid").read_text())
    assert worker != int((root / ".dev-session/pgid").read_text())
    process.terminate()
    process.communicate(timeout=10)
    assert process.returncode == 143
    assert not process_active(worker)
    assert not (root / ".dev-session").exists()
