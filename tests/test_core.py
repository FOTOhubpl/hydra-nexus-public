"""Storage, database writer and audit chain."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3

import pytest

from hydra import events
from hydra.config import Settings
from hydra.db import MIGRATIONS, Writer, connect, schema_version
from hydra.storage import ContentStore


def test_cas_roundtrip_is_content_addressed_and_read_only(settings: Settings):
    store = ContentStore(settings.cas_dir)
    data = b"hello hydra"
    digest = store.put_bytes(data)
    assert digest == hashlib.sha256(data).hexdigest()
    path = store.path_for(digest)
    assert path.read_bytes() == data
    assert not os.access(path, os.W_OK) or os.getuid() == 0
    assert store.put_bytes(data) == digest  # idempotent
    assert list(store.iter_digests()) == [digest]
    assert store.verify(digest)


def test_cas_rejects_oversize_and_leaves_no_partial_file(settings: Settings):
    store = ContentStore(settings.cas_dir)
    with pytest.raises(ValueError):
        store.put_stream(io.BytesIO(b"x" * 2048), max_bytes=1024)
    assert list(store.iter_digests()) == []
    assert not list(settings.cas_dir.glob(".incoming-*"))


def test_cas_rejects_bad_digest(settings: Settings):
    with pytest.raises(ValueError):
        ContentStore(settings.cas_dir).path_for("../../etc/passwd")


def test_writer_migrates_and_rolls_back_failed_transactions(settings: Settings):
    w = Writer(settings.db_path)
    try:
        with connect(settings.db_path, readonly=True) as conn:
            assert schema_version(conn) == MIGRATIONS[-1][0]

        def boom(conn: sqlite3.Connection) -> None:
            events.append(conn, "TEST", {"n": 1})
            raise RuntimeError("fail after write")

        with pytest.raises(RuntimeError):
            w.run(boom)
        assert w.run(lambda c: c.execute("SELECT COUNT(*) FROM events").fetchone()[0]) == 0
    finally:
        w.close()


def test_audit_chain_detects_tampering(settings: Settings):
    w = Writer(settings.db_path)
    try:
        for i in range(5):
            w.run(lambda c, i=i: events.append(c, "TEST", {"i": i}))
        with connect(settings.db_path, readonly=True) as conn:
            assert events.verify_chain(conn) == (True, 5, None)
        w.run(lambda c: c.execute("UPDATE events SET payload='{\"i\":99}' WHERE seq=3"))
        with connect(settings.db_path, readonly=True) as conn:
            ok, _, bad = events.verify_chain(conn)
        assert not ok and bad == 3
    finally:
        w.close()


def test_canonical_json_is_order_independent():
    assert events.canonical_json({"b": 1, "a": "ł"}) == events.canonical_json({"a": "ł", "b": 1}) == '{"a":"ł","b":1}'


def test_settings_refuse_public_bind(tmp_path, monkeypatch):
    monkeypatch.delenv("HYDRA_IN_CONTAINER", raising=False)
    with pytest.raises(ValueError):
        Settings(root=tmp_path, api_host="0.0.0.0")
    # compose service `gui` (profile gui-ports): own network namespace, port published on the host's 127.0.0.1
    monkeypatch.setenv("HYDRA_IN_CONTAINER", "1")
    assert Settings(root=tmp_path, api_host="0.0.0.0").api_host == "0.0.0.0"
    for host in ("192.168.1.10", "::", "example.org"):  # still nothing else, even in a container
        with pytest.raises(ValueError):
            Settings(root=tmp_path, api_host=host)
    monkeypatch.setenv("HYDRA_IN_CONTAINER", "true")  # exactly "1"
    with pytest.raises(ValueError):
        Settings(root=tmp_path, api_host="0.0.0.0")


def test_writer_survives_fn_that_commits(tmp_path):
    from hydra.db import Writer

    w = Writer(tmp_path / "w.db")
    try:
        def bad(conn):
            conn.execute("COMMIT")  # leaves no active transaction
            raise ValueError("boom")

        with pytest.raises(ValueError):
            w.run(bad)
        assert w.run(lambda c: c.execute("SELECT 1").fetchone()[0]) == 1
    finally:
        w.close()


def test_writer_reentrant_submit_raises_instead_of_deadlock(tmp_path):
    from hydra.db import Writer

    w = Writer(tmp_path / "w.db")
    try:
        with pytest.raises(RuntimeError, match="writer thread"):
            w.run(lambda c: w.run(lambda c2: 1), timeout=5)
        assert w.run(lambda c: 2) == 2
    finally:
        w.close()


def test_writer_migration_error_raises_in_init(tmp_path, monkeypatch):
    import hydra.db as db

    def broken(conn):
        raise sqlite3.OperationalError("migration failed")

    monkeypatch.setattr(db, "migrate", broken)
    with pytest.raises(sqlite3.OperationalError):
        db.Writer(tmp_path / "w.db")
