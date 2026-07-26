"""Storage-layer tests: schema, path resolution, write locking, tick file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_schema_created_at_version_1(mods, conn):
    _config, db, _chat = mods
    assert db.get_schema_version(conn) == db.SCHEMA_VERSION == 1
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"parties", "channels", "channel_parties", "messages",
            "item_consumers", "cursors", "schema_version"} <= tables


def test_ensure_schema_is_idempotent(mods, conn):
    _config, db, _chat = mods
    db.ensure_schema(conn)
    db.ensure_schema(conn)
    assert db.get_schema_version(conn) == db.SCHEMA_VERSION


def test_db_path_follows_env(mods, tmp_path, monkeypatch):
    _config, db, _chat = mods
    target = tmp_path / "nested" / "other.db"
    monkeypatch.setenv("MCPP_CHAT_DB", str(target))
    assert db.resolve_db_path() == target
    conn = db.open_db()
    conn.close()
    assert target.exists()


def test_pragmas_are_set(mods, conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_writing_rolls_back_on_error(mods, conn, parties):
    _config, db, _chat = mods
    before = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    with pytest.raises(RuntimeError):
        with db.writing(conn):
            conn.execute(
                "INSERT INTO channels (name, created_at, is_closed) VALUES ('doomed', 'now', 0)"
            )
            raise RuntimeError("boom")
    after = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    assert after == before


def test_two_processes_get_distinct_seqs(mods, conn, parties, channel):
    """Two independent connections posting to one channel never collide on seq."""
    _config, db, chat = mods
    pa, pb = parties
    other = db.open_db()
    try:
        ch = chat.resolve_channel(conn, pa, channel)
        first = chat._insert(conn, int(ch["id"]), int(pa["id"]), "say", "from A")
        second = chat._insert(other, int(ch["id"]), int(pb["id"]), "say", "from B")
    finally:
        other.close()
    assert first["seq"] != second["seq"]
    rows = conn.execute(
        "SELECT seq FROM messages WHERE channel_id = ? ORDER BY seq", (ch["id"],)
    ).fetchall()
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(set(seqs))


def test_seq_uniqueness_is_enforced_by_the_index(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, _pb = parties
    ch = chat.resolve_channel(conn, pa, channel)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages (channel_id, seq, kind, body, author_id, status, is_archived, "
            "created_at, updated_at) VALUES (?, 1, 'say', 'x', ?, 'none', 0, 'now', 'now')",
            (ch["id"], pa["id"]),
        )
        conn.execute(
            "INSERT INTO messages (channel_id, seq, kind, body, author_id, status, is_archived, "
            "created_at, updated_at) VALUES (?, 1, 'say', 'y', ?, 'none', 0, 'now', 'now')",
            (ch["id"], pa["id"]),
        )


def test_tick_file_written_on_post(mods, conn, parties, channel, tmp_path):
    _config, db, chat = mods
    pa, _pb = parties
    chat.say(conn, pa, "hello", channel)
    tick = db.tick_path()
    assert tick is not None and tick.exists()
    line = tick.read_text(encoding="utf-8").strip().split()
    assert line[1] == "link" and line[3] == pa["name"]


def test_tick_file_can_be_disabled(mods, conn, parties, channel, tmp_path, monkeypatch):
    config, db, chat = mods
    cfg_file = tmp_path / "off.yaml"
    cfg_file.write_text('chat:\n  tick_file: ""\n', encoding="utf-8")
    monkeypatch.setenv("MCPP_CHAT_CONFIG", str(cfg_file))
    assert db.tick_path() is None
    pa, _pb = parties
    chat.say(conn, pa, "no tick please", channel)  # must not raise


def test_tick_overwrite_keeps_file_small(mods, conn, parties, channel):
    _config, db, chat = mods
    pa, _pb = parties
    for i in range(20):
        chat.say(conn, pa, f"msg {i}", channel)
    assert len(db.tick_path().read_text(encoding="utf-8").splitlines()) == 1


def test_daily_backup_is_created_once(mods, tmp_path):
    _config, db, _chat = mods
    conn = db.open_db()
    conn.close()
    conn = db.open_db()          # second open: DB now exists, backup runs
    conn.close()
    backups = list((tmp_path / ".backups").glob("chat.db.*"))
    assert len(backups) == 1
    conn = db.open_db()          # third open: same day, no duplicate
    conn.close()
    assert len(list((tmp_path / ".backups").glob("chat.db.*"))) == 1
