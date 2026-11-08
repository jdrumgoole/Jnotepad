"""Database lifecycle: a managed SecantusDB server, or an external MongoDB-wire URL."""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jnotepad._supervisor import STOP_TIMEOUT
from jnotepad.config import Settings

log = logging.getLogger(__name__)

RUST_BINARY = "secantusd-rs"
# The version this app is tested against. Newer betas will probably work, but
# SecantusDB is pre-1.0, so a mismatch is logged.
TESTED_RUST_VERSION = "0.5.3-beta.196"
INSTALL_HINT = (
    f"Install it with: cargo install secantus-mdb --version {TESTED_RUST_VERSION} "  # noqa: S608
    "--locked "
    "(needs rustc >= 1.98; `rustup update stable` first), or set JNOTEPAD_SECANTUSD_BIN, "
    "or run with JNOTEPAD_ENGINE=python."
)
_READY_PREFIX = f"{RUST_BINARY} listening on "
_STOP_TIMEOUT = STOP_TIMEOUT
_SUPERVISOR = Path(__file__).with_name("_supervisor.py")


@contextmanager
def database_server(settings: Settings) -> Iterator[str]:
    """Yield a ``mongodb://`` base URI (no database path) for the app's lifetime."""
    if settings.mongodb_url:
        yield settings.mongodb_url.rstrip("/")
        return
    settings.secantus_path.mkdir(parents=True, exist_ok=True)
    if settings.engine == "python":
        with _python_server(settings) as uri:
            yield uri
    else:
        with _rust_server(settings) as uri:
            yield uri


def find_rust_binary(settings: Settings) -> str:
    candidates = [settings.secantusd_bin] if settings.secantusd_bin else [
        shutil.which(RUST_BINARY),
        str(Path.home() / ".cargo" / "bin" / RUST_BINARY),
    ]
    for c in candidates:
        if c and os.access(c, os.X_OK) and Path(c).is_file():
            return c
    where = settings.secantusd_bin or "$PATH or ~/.cargo/bin"
    raise RuntimeError(f"{RUST_BINARY} not found ({where}). {INSTALL_HINT}")


@contextmanager
def _rust_server(settings: Settings) -> Iterator[str]:
    binary = find_rust_binary(settings)
    version = subprocess.run([binary, "--version"], capture_output=True, text=True,  # noqa: S603
                             timeout=10).stdout.split()
    if TESTED_RUST_VERSION not in version:
        log.warning("%s version %s; Jnotepad is tested against %s", binary,
                    " ".join(version[1:2]) or "unknown", TESTED_RUST_VERSION)

    cmd = [
        binary,
        "--host", "127.0.0.1",
        "--port", "0",  # ephemeral; the real port is read from the ready line
        "--storage-path", str(settings.secantus_path.resolve()),
        "--cache-size", settings.secantus_cache_size,
        "--log-level", "WARNING",
    ]
    log.info("Starting %s", " ".join(cmd))
    # The server runs under a small supervisor whose stdin is a pipe from us.
    # Whatever way this process ends, the OS closes that pipe and the
    # supervisor stops the server cleanly: see jnotepad/_supervisor.py.
    # -I plus a file path: the supervisor needs nothing from jnotepad and must
    # not pick up modules from the current directory.
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", str(_SUPERVISOR), *cmd],
        stdin=subprocess.PIPE,  # never written; closing it means "stop"
        stdout=subprocess.PIPE,  # the server's stdout, passed through
        stderr=None,  # server and supervisor logs go straight to our stderr
        text=True,
        bufsize=1,
    )
    try:
        address = _wait_until_ready(proc, settings.secantus_startup_timeout)
        log.info("SecantusDB (Rust) listening on %s (store: %s)", address,
                 settings.secantus_path)
        yield f"mongodb://{address}"
    finally:
        _stop(proc)


def _wait_until_ready(proc: subprocess.Popen[str], timeout: float) -> str:
    """Read stdout until the ready line; keep draining afterwards so the pipe
    can never fill up and block the server."""
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        ready = False
        for line in proc.stdout:
            if ready:
                log.info("%s: %s", RUST_BINARY, line.rstrip())
                continue
            ready = line.startswith(_READY_PREFIX)
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, name="secantusd-stdout", daemon=True).start()

    seen: list[str] = []
    while True:
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"{RUST_BINARY} did not report ready within {timeout:.0f}s") \
                from None
        if line is None:
            code = proc.wait()
            raise RuntimeError(
                f"{RUST_BINARY} exited with code {code} before it was ready "
                f"(see its stderr above; a held lock usually means another server is "
                f"already using this data directory). Output: {''.join(seen).strip()!r}")
        if line.startswith(_READY_PREFIX):
            return line[len(_READY_PREFIX):].strip()
        seen.append(line)


def _stop(proc: subprocess.Popen[str]) -> None:
    """Close the supervisor's stdin (the stop signal) and wait for it; it stops
    the server, including the WiredTiger checkpoint, before exiting."""
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=_STOP_TIMEOUT + 10)  # supervisor's own grace period + slack
    except subprocess.TimeoutExpired:
        log.error("SecantusDB supervisor did not exit; killing it. %s may still be "
                  "running and holding %s", RUST_BINARY, "the data directory lock")
        proc.kill()
        proc.wait()


@contextmanager
def _python_server(settings: Settings) -> Iterator[str]:
    try:
        from secantus import SecantusDBServer
    except ImportError as exc:
        raise RuntimeError(
            "JNOTEPAD_ENGINE=python needs the Python server: "
            "pip install 'jnotepad[python-server]'") from exc
    with SecantusDBServer(storage_path=str(settings.secantus_path)) as server:
        log.info("SecantusDB (Python) listening on %s (store: %s)", server.uri,
                 settings.secantus_path)
        yield server.uri.rstrip("/")


async def install_indexes(db) -> None:
    await db.items.create_index([("owner_id", 1), ("updated_at", -1)])
    await db.items.create_index([("owner_id", 1), ("tags", 1)])
    await db.images.create_index([("owner_id", 1), ("item_id", 1)])
