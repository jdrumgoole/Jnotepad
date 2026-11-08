"""Tiny supervisor that ties the database server's lifetime to the app's.

Usage (by jnotepad.db, not by hand)::

    python -m jnotepad._supervisor <server argv...>

The app holds the write end of this process's stdin. The OS closes that pipe
when the app exits for *any* reason (clean exit, crash, SIGKILL, Task Manager
"End task", OOM killer), on every platform. On EOF the supervisor asks the
server to stop gracefully, so WiredTiger checkpoints and releases its
data-directory lock, and kills it only if it doesn't stop in time.

This replaces OS-specific tricks (Linux PR_SET_PDEATHSIG, Windows job objects,
macOS kqueue) with one mechanism that behaves the same everywhere. That also
means the Linux test suite exercises the same code path macOS and Windows run.

The server's stdout is inherited, so the app reads the server's own
"listening on" line straight through this process.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading

STOP_TIMEOUT = 30.0  # graceful stop includes a WiredTiger checkpoint
WINDOWS = sys.platform == "win32"


def _linux_pdeathsig() -> None:  # pragma: no cover - runs in the child
    """Extra layer on Linux: if *this* supervisor is SIGKILLed, the server still
    gets SIGTERM. (Elsewhere a killed supervisor is the one remaining gap.)"""
    import ctypes

    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def start_server(cmd: list[str]) -> subprocess.Popen[bytes]:
    kwargs: dict = {}
    if WINDOWS:
        # Own process group, so CTRL_BREAK_EVENT reaches only the server.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    elif sys.platform == "linux":
        kwargs["preexec_fn"] = _linux_pdeathsig
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, **kwargs)  # noqa: S603


def request_stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if WINDOWS:
        # secantusd-rs handles Ctrl+Break (via the `ctrlc` crate) as a clean stop.
        # Popen.terminate() would be TerminateProcess: no checkpoint.
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()  # SIGTERM -> clean stop


def stop(proc: subprocess.Popen[bytes], timeout: float = STOP_TIMEOUT) -> int:
    request_stop(proc)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"jnotepad supervisor: server did not stop in {timeout:.0f}s; killing it",
              file=sys.stderr, flush=True)
        proc.kill()
        return proc.wait()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m jnotepad._supervisor <command...>", file=sys.stderr)
        return 2

    stop_requested = threading.Event()

    # Ctrl-C in a terminal reaches the whole foreground process group: the
    # server stops itself and the app closes our stdin. Dying here first would
    # only lose track of the server, so ignore it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if not WINDOWS:
        signal.signal(signal.SIGTERM, lambda *_: stop_requested.set())

    proc = start_server(argv)

    def watch_parent() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass  # the app never writes; anything sent is ignored
        except OSError:
            pass
        stop_requested.set()  # EOF: the app has gone, one way or another

    threading.Thread(target=watch_parent, name="parent-watch", daemon=True).start()

    while proc.poll() is None:
        if stop_requested.wait(timeout=0.5):
            return stop(proc)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
