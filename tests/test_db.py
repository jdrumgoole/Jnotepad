"""The managed secantusd-rs process: startup, shutdown, persistence, failure modes."""

from __future__ import annotations

import socket

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
