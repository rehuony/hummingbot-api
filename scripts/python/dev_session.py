"""Supervise the Conda development command and all workers in its process group."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

GRACE_SECONDS = 5


def group_active(pgid):
    """Treat an unreadable process table conservatively; zombies cannot keep serving."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can report EPERM while a process group is disappearing.
        pass
    try:
        result = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return True
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            return True
        if int(fields[0]) == pgid and not fields[1].startswith("Z"):
            return True
    return False


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        # The process-table check keeps the guard if signaling was denied.
        pass


def wait_for_group(child, seconds):
    deadline = time.monotonic() + seconds
    while True:
        child.poll()  # Reap the group leader before checking for remaining workers.
        if not group_active(child.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def run_session(command, session):
    received_signal = None
    child = None
    result = 1

    def request_stop(signum, _frame):
        nonlocal received_signal
        received_signal = received_signal or signum

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    try:
        (session / "pid").write_text(f"{os.getpid()}\n")
        child = subprocess.Popen(command, cwd=session.parent, start_new_session=True)
        (session / "pgid").write_text(f"{child.pid}\n")
        while child.poll() is None and received_signal is None:
            time.sleep(0.05)
        if child.returncode is not None:
            result = child.returncode if child.returncode >= 0 else 128 - child.returncode
    except OSError:
        print("Error: Could not start the development session.", file=sys.stderr)
    finally:
        stopped = child is None
        if child is not None:
            signal_group(child.pid, received_signal or signal.SIGTERM)
            stopped = wait_for_group(child, GRACE_SECONDS)
            if not stopped:
                signal_group(child.pid, signal.SIGKILL)
                stopped = wait_for_group(child, 2)
        if stopped:
            for name in ("pid", "pgid"):
                (session / name).unlink(missing_ok=True)
            session.rmdir()
        else:
            print("Error: Development workers remain active; the session guard was retained.", file=sys.stderr)
            result = 1
    return 128 + received_signal if received_signal else result


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    if len(sys.argv) < 2:
        raise SystemExit("Usage: dev_session.py COMMAND [ARG ...]")
    raise SystemExit(run_session(sys.argv[1:], root / ".dev-session"))
