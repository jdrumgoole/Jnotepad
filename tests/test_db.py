"""The managed secantusd-rs process: startup, shutdown, persistence, failure modes."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest
from pymongo import MongoClient

from jnotepad.config import Settings
from jnotepad.db import database_server, find_rust_binary

pytestmark = pytest.mark.skipif(Settings().engine != "rust", reason="Rust engine only")


def _port_open(uri: str) -> bool:
    host, port = uri.removeprefix("mongodb://").rsplit(":", 1)
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, int(port))) == 0


def test_data_survives_restart_and_lock_is_released(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with database_server(settings) as uri:
        with MongoClient(uri, serverSelectionTimeoutMS=5000) as c:
            c.t.docs.insert_one({"_id": 1, "v": "kept"})
    assert not _port_open(uri), "server should be stopped when the context exits"

    # Reopening the same directory only works if the first server shut down
    # cleanly and released WiredTiger's exclusive lock.
    with database_server(settings) as uri2:
        with MongoClient(uri2, serverSelectionTimeoutMS=5000) as c:
            assert c.t.docs.find_one({"_id": 1})["v"] == "kept"


def test_second_server_on_same_dir_fails_clearly(tmp_path):
    settings = Settings(data_dir=tmp_path, secantus_startup_timeout=15)
    with database_server(settings):
        with pytest.raises(RuntimeError, match="exited with code"):
            with database_server(settings):
                pass


def test_missing_binary_gives_install_hint(tmp_path):
    settings = Settings(data_dir=tmp_path, secantusd_bin=str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="cargo install secantus-mdb"):
        find_rust_binary(settings)


def test_external_url_bypasses_engine(tmp_path):
    settings = Settings(data_dir=tmp_path, mongodb_url="mongodb://db.example:27017/",
                        secantusd_bin=str(tmp_path / "nope"))
    with database_server(settings) as uri:
        assert uri == "mongodb://db.example:27017"


_APP = """
import sys, time
from pymongo import MongoClient
from jnotepad.config import Settings
from jnotepad.db import database_server
with database_server(Settings(data_dir=sys.argv[1])) as uri:
    with MongoClient(uri) as c:
        c.t.docs.insert_one({"_id": "before-kill"})
    print(uri, flush=True)
    time.sleep(600)
"""


def _wait_closed(uri: str, seconds: float = 40) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _port_open(uri):
            return True
        time.sleep(0.2)
    return False


def _spawn_app(data_dir) -> tuple[subprocess.Popen[str], str]:
    app = subprocess.Popen([sys.executable, "-c", _APP, str(data_dir)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    uri = app.stdout.readline().strip()
    assert uri.startswith("mongodb://"), app.stderr.read()
    return app, uri


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal")
def test_server_stops_cleanly_when_app_is_sigkilled(tmp_path):
    """The app dies without running any cleanup. The supervisor must notice
    (its stdin pipe closes) and stop the server gracefully. This is the
    portable path; nothing Linux-specific is involved, because only the
    app is killed."""
    app, uri = _spawn_app(tmp_path)
    os.kill(app.pid, signal.SIGKILL)
    assert _wait_closed(uri), "server outlived the app"
    # stderr stays open until the supervisor and server (which inherit it) exit.
    _, err = app.communicate(timeout=40)
    assert "killing it" not in err, f"server was force-killed, not stopped: {err}"

    # Graceful stop released the lock and kept the data.
    with database_server(Settings(data_dir=tmp_path)) as uri2:
        with MongoClient(uri2, serverSelectionTimeoutMS=5000) as c:
            assert c.t.docs.find_one({"_id": "before-kill"}) is not None


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux-only")
def test_server_stops_when_supervisor_is_sigkilled(tmp_path):
    app, uri = _spawn_app(tmp_path)
    try:
        sup = subprocess.run(["pgrep", "-P", str(app.pid), "-f", "_supervisor.py"],
                             capture_output=True, text=True).stdout.split()
        assert len(sup) == 1, sup
        os.kill(int(sup[0]), signal.SIGKILL)
        assert _wait_closed(uri), "server outlived its supervisor"
    finally:
        app.kill()
        app.communicate(timeout=40)
