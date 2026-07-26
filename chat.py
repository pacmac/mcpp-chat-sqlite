"""Protocol logic for mcpp-chat.

Two weights of message:

  say / reply  — chatter. Consumed by reading; the cursor makes it invisible.
  ask          — a contract. OPEN -> ACK -> DONE, then archived only once every
                 consumer has independently said it is safe to prune.

Every function here takes an open connection and a party dict, and returns
JSON-serialisable data. Errors are raised as ChatError and turned into
{"success": False, ...} by mcpptool.py.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .config import get_config
from .db import utc_now_iso, write_tick, writing


class ChatError(Exception):
    """A user-facing protocol or usage error."""


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._~-]{0,63}$")


# ── Helpers ──

def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:max_len].strip("-")
    return s or "item"


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def _os_user() -> str:
    return (os.environ.get("USER") or os.environ.get("USERNAME") or "unknown").lower()


# ── Parties ──

def party_for(conn: sqlite3.Connection, workspace_dir: str) -> dict:
    """Upsert and return the party for this workspace. Identity is the repo.

    The party name is the repo basename (or identity.name from config), made
    unique with a ~N suffix if some other workspace already claimed it.
    workspace_path is the real key and is never rewritten.
    """
    path = str(Path(workspace_dir).expanduser().resolve())
    now = utc_now_iso()

    existing = conn.execute(
        "SELECT * FROM parties WHERE workspace_path = ?", (path,)
    ).fetchone()
    if existing:
        conn.execute("UPDATE parties SET last_seen = ? WHERE id = ?", (now, existing["id"]))
        out = dict(existing)
        out["last_seen"] = now
        return out

    configured = str(get_config().get("identity", {}).get("name", "") or "").strip().lower()
    base = configured or Path(path).name.lower() or "unnamed"
    base = re.sub(r"[^a-z0-9._~-]+", "-", base).strip("-") or "unnamed"

    name = base
    n = 1
    while conn.execute("SELECT 1 FROM parties WHERE name = ?", (name,)).fetchone():
        n += 1
        name = f"{base}~{n}"

    with writing(conn):
        cur = conn.execute(
            "INSERT INTO parties (name, workspace_path, os_user, about, first_seen, last_seen) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (name, path, _os_user(), now, now),
        )
        party_id = int(cur.lastrowid)
    return _row(conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())  # type: ignore[return-value]


def set_about(conn: sqlite3.Connection, party: dict, about: str) -> dict:
    with writing(conn):
        conn.execute("UPDATE parties SET about = ? WHERE id = ?", (about, party["id"]))
    return _row(conn.execute("SELECT * FROM parties WHERE id = ?", (party["id"],)).fetchone())  # type: ignore[return-value]


def party_by_name(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM parties WHERE name = ?", (name.strip().lower(),)).fetchone())


def party_for_path(conn: sqlite3.Connection, some_path: str) -> Optional[dict]:
    """The party owning `some_path`, walking up to the first known ancestor.

    A session's cwd moves during a turn, so an exact match on workspace_path
    misses any party whose session has stepped into a subdirectory. Read-only:
    unlike party_for() this never creates a party, because a stray path must not
    mint one.
    """
    try:
        here = Path(some_path).expanduser().resolve()
    except OSError:
        return None
    for candidate in (here, *here.parents):
        row = conn.execute(
            "SELECT * FROM parties WHERE workspace_path = ?", (str(candidate),)
        ).fetchone()
        if row is not None:
            return dict(row)
    return None


# ── Being live ──
#
# A session seated in an open channel is reachable by its peers. It is not a
# mode and there is nothing to switch on: joining a channel is the opt-in, and
# chat_channel_leave is the only opt-out.

def is_live(conn: sqlite3.Connection, party: dict) -> bool:
    """True when this repo is seated in at least one open channel."""
    if not bool(get_config().get("chat", {}).get("always_live", True)):
        return False
    return bool(my_channels(conn, int(party["id"])))


# ── Channels ──

def _channel_by_name(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM channels WHERE name = ?", (name.strip().lower(),)).fetchone())


def channel_parties(conn: sqlite3.Connection, channel_id: int, include_left: bool = False) -> list[dict]:
    sql = (
        "SELECT p.id, p.name, p.workspace_path, p.about, cp.joined_at, cp.left_at "
        "FROM channel_parties cp JOIN parties p ON p.id = cp.party_id "
        "WHERE cp.channel_id = ?"
    )
    if not include_left:
        sql += " AND cp.left_at IS NULL"
    sql += " ORDER BY cp.joined_at, p.name"
    return [dict(r) for r in conn.execute(sql, (channel_id,)).fetchall()]


def _is_seated(conn: sqlite3.Connection, channel_id: int, party_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM channel_parties WHERE channel_id = ? AND party_id = ? AND left_at IS NULL",
        (channel_id, party_id),
    ).fetchone()
    return row is not None


def my_channels(conn: sqlite3.Connection, party_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.* FROM channels c JOIN channel_parties cp ON cp.channel_id = c.id "
        "WHERE cp.party_id = ? AND cp.left_at IS NULL AND c.is_closed = 0 "
        "ORDER BY c.name",
        (party_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_channel(conn: sqlite3.Connection, party: dict, name: str,
                   about: str | None = None, parties: list[str] | None = None) -> dict:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ChatError(
            f"invalid channel name {name!r}: use lowercase letters, digits, '.', '_', '~', '-' "
            "(e.g. 'mesh-gw--node-dash')"
        )
    if _channel_by_name(conn, name):
        raise ChatError(f"channel '{name}' already exists — join it with chat_channel_join")

    seats: list[int] = [int(party["id"])]
    unknown: list[str] = []
    for other in parties or []:
        row = party_by_name(conn, other)
        if row is None:
            unknown.append(other)
        elif int(row["id"]) not in seats:
            seats.append(int(row["id"]))
    if unknown:
        raise ChatError(
            f"unknown parties: {', '.join(unknown)}. A party exists once its session has used any "
            "chat_ tool from its own repo; it can also join later with chat_channel_join."
        )

    now = utc_now_iso()
    with writing(conn):
        cur = conn.execute(
            "INSERT INTO channels (name, about, created_by, created_at, is_closed) VALUES (?, ?, ?, ?, 0)",
            (name, about, party["id"], now),
        )
        channel_id = int(cur.lastrowid)
        for pid in seats:
            conn.execute(
                "INSERT INTO channel_parties (channel_id, party_id, joined_at) VALUES (?, ?, ?)",
                (channel_id, pid, now),
            )
    return channel_summary(conn, party, channel_id)


def join_channel(conn: sqlite3.Connection, party: dict, name: str) -> dict:
    channel = _channel_by_name(conn, name)
    if channel is None:
        raise ChatError(f"no channel named '{name}' — create it with chat_channel_new")
    now = utc_now_iso()
    with writing(conn):
        conn.execute(
            "INSERT INTO channel_parties (channel_id, party_id, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id, party_id) DO UPDATE SET left_at = NULL, joined_at = excluded.joined_at",
            (channel["id"], party["id"], now),
        )
    _event(conn, int(channel["id"]), party, f"{party['name']} joined the channel")
    return channel_summary(conn, party, int(channel["id"]))


def leave_channel(conn: sqlite3.Connection, party: dict, name: str) -> dict:
    channel = _channel_by_name(conn, name)
    if channel is None:
        raise ChatError(f"no channel named '{name}'")
    if not _is_seated(conn, int(channel["id"]), int(party["id"])):
        raise ChatError(f"you are not in '{name}'")
    with writing(conn):
        conn.execute(
            "UPDATE channel_parties SET left_at = ? WHERE channel_id = ? AND party_id = ?",
            (utc_now_iso(), channel["id"], party["id"]),
        )
    _event(conn, int(channel["id"]), party,
           f"{party['name']} left the channel (existing item obligations remain)")
    return {"channel": channel["name"], "left": True}


def resolve_channel(conn: sqlite3.Connection, party: dict, name: str | None) -> dict:
    """Resolve the channel to act on.

    Explicit name wins. Otherwise: the single channel I am in; or the configured
    default if I am in it. Never guesses between candidates.
    """
    if name:
        channel = _channel_by_name(conn, name)
        if channel is None:
            raise ChatError(f"no channel named '{name}' — create it with chat_channel_new")
        if not _is_seated(conn, int(channel["id"]), int(party["id"])):
            raise ChatError(
                f"you are not in '{channel['name']}' — join it with chat_channel_join first"
            )
        return channel

    mine = my_channels(conn, int(party["id"]))
    if not mine:
        raise ChatError(
            "you are not in any channel yet — create one with "
            "chat_channel_new(name=..., parties=[...])"
        )
    if len(mine) == 1:
        return mine[0]

    default = str(get_config().get("chat", {}).get("default_channel", "") or "").strip().lower()
    for c in mine:
        if c["name"] == default:
            return c
    names = ", ".join(c["name"] for c in mine)
    raise ChatError(f"you are in several channels — pass channel=<one of: {names}>")


def channel_summary(conn: sqlite3.Connection, party: dict, channel_id: int) -> dict:
    channel = _row(conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone())
    if channel is None:
        raise ChatError("channel vanished")
    parties = channel_parties(conn, channel_id)
    last_seq = _cursor_seq(conn, channel_id, int(party["id"]))
    unread = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE channel_id = ? AND seq > ? AND is_archived = 0 "
        "AND author_id != ?",
        (channel_id, last_seq, party["id"]),
    ).fetchone()["n"]
    open_items = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE channel_id = ? AND kind = 'ask' "
        "AND is_archived = 0 AND status IN ('open','ack','done')",
        (channel_id,),
    ).fetchone()["n"]
    return {
        "channel": channel["name"],
        "about": channel["about"],
        "parties": [p["name"] for p in parties],
        "unread": int(unread),
        "live_items": int(open_items),
        "created_at": channel["created_at"],
    }


def list_channels(conn: sqlite3.Connection, party: dict, show_all: bool = False) -> list[dict]:
    if show_all:
        rows = conn.execute("SELECT * FROM channels ORDER BY name").fetchall()
        channels = [dict(r) for r in rows]
    else:
        channels = my_channels(conn, int(party["id"]))
    out = []
    for c in channels:
        summary = channel_summary(conn, party, int(c["id"]))
        summary["member"] = _is_seated(conn, int(c["id"]), int(party["id"]))
        summary["closed"] = bool(c["is_closed"])
        out.append(summary)
    return out


# ── Cursors ──

def _cursor_seq(conn: sqlite3.Connection, channel_id: int, party_id: int) -> int:
    row = conn.execute(
        "SELECT last_seq FROM cursors WHERE channel_id = ? AND party_id = ?",
        (channel_id, party_id),
    ).fetchone()
    return int(row["last_seq"]) if row else 0


def _set_cursor(conn: sqlite3.Connection, channel_id: int, party_id: int, seq: int) -> None:
    with writing(conn):
        conn.execute(
            "INSERT INTO cursors (channel_id, party_id, last_seq, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel_id, party_id) DO UPDATE SET "
            "last_seq = MAX(last_seq, excluded.last_seq), updated_at = excluded.updated_at",
            (channel_id, party_id, seq, utc_now_iso()),
        )


# ── Messages ──

def _insert(conn: sqlite3.Connection, channel_id: int, author_id: int, kind: str, body: str,
            *, title: str | None = None, slug: str | None = None, ref: str | None = None,
            parent_id: int | None = None, status: str = "none") -> dict:
    """Insert one message, allocating the per-channel seq under the write lock."""
    now = utc_now_iso()
    with writing(conn):
        seq = int(conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM messages WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()["s"])
        cur = conn.execute(
            "INSERT INTO messages (channel_id, seq, kind, parent_id, slug, title, body, ref, "
            "author_id, status, is_archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (channel_id, seq, kind, parent_id, slug, title, body, ref, author_id, status, now, now),
        )
        message_id = int(cur.lastrowid)
    # Every insert ticks, so a watcher sees state changes (acks, resolves,
    # archives) and not only fresh prose.
    write_tick(_channel_name(conn, channel_id), seq, _party_name(conn, author_id))
    return {"id": message_id, "seq": seq}


def _event(conn: sqlite3.Connection, channel_id: int, party: dict, text: str) -> None:
    _insert(conn, channel_id, int(party["id"]), "event", text)


def _hydrate(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict:
    r = dict(row)
    author = conn.execute("SELECT name FROM parties WHERE id = ?", (r["author_id"],)).fetchone()
    channel = conn.execute("SELECT name FROM channels WHERE id = ?", (r["channel_id"],)).fetchone()
    out = {
        "channel": channel["name"] if channel else "?",
        "seq": r["seq"],
        "kind": r["kind"],
        "from": author["name"] if author else "?",
        "body": r["body"],
        "at": r["created_at"],
    }
    for key in ("slug", "title", "ref"):
        if r.get(key):
            out[key] = r[key]
    if r["kind"] == "ask":
        out["status"] = r["status"]
        out["archived"] = bool(r["is_archived"])
        out["consumers"] = _consumer_state(conn, int(r["id"]))
    if r.get("parent_id"):
        parent = conn.execute(
            "SELECT seq, slug, title FROM messages WHERE id = ?", (r["parent_id"],)
        ).fetchone()
        if parent:
            out["re"] = parent["slug"] or f"#{parent['seq']}"
    return out


def _consumer_state(conn: sqlite3.Connection, message_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT p.name, ic.acked_at, ic.prune_ok_at FROM item_consumers ic "
        "JOIN parties p ON p.id = ic.party_id WHERE ic.message_id = ? ORDER BY p.name",
        (message_id,),
    ).fetchall()
    return [
        {"party": r["name"], "acked": bool(r["acked_at"]), "prune_ok": bool(r["prune_ok_at"])}
        for r in rows
    ]


def say(conn: sqlite3.Connection, party: dict, text: str, channel: str | None = None,
        reply_to: str | None = None, ref: str | None = None) -> dict:
    if not (text or "").strip():
        raise ChatError("text is required")
    if reply_to:
        return reply(conn, party, reply_to, text, channel=channel)
    ch = resolve_channel(conn, party, channel)
    rec = _insert(conn, int(ch["id"]), int(party["id"]), "say", text.strip(), ref=ref)
    _set_cursor(conn, int(ch["id"]), int(party["id"]), rec["seq"])
    return {"channel": ch["name"], "seq": rec["seq"], "kind": "say",
            "to": [p["name"] for p in channel_parties(conn, int(ch["id"])) if p["id"] != party["id"]]}


def ask(conn: sqlite3.Connection, party: dict, title: str, body: str, channel: str | None = None,
        slug: str | None = None, ref: str | None = None, consumers: list[str] | None = None,
        announce: bool = False) -> dict:
    if not (title or "").strip():
        raise ChatError("title is required")
    if not (body or "").strip():
        raise ChatError("body is required — state what you need and what you observe from your side")
    ch = resolve_channel(conn, party, channel)
    channel_id = int(ch["id"])

    want = slugify(slug or title)
    final = want
    n = 1
    while conn.execute(
        "SELECT 1 FROM messages WHERE channel_id = ? AND slug = ?", (channel_id, final)
    ).fetchone():
        n += 1
        final = f"{want}-{n}"

    seated = channel_parties(conn, channel_id)
    seated_by_name = {p["name"]: p for p in seated}
    if consumers:
        chosen = []
        for name in consumers:
            key = name.strip().lower()
            if key not in seated_by_name:
                raise ChatError(
                    f"'{name}' is not in channel '{ch['name']}' (parties: "
                    f"{', '.join(seated_by_name) or 'none'})"
                )
            chosen.append(seated_by_name[key])
        if party["name"] not in {c["name"] for c in chosen}:
            chosen.append(seated_by_name[party["name"]])
    else:
        chosen = seated
    if len(chosen) < 2:
        raise ChatError(
            f"channel '{ch['name']}' has no other party to ask — add one with chat_channel_join "
            "from that repo's session, or name parties when creating the channel"
        )

    status = "done" if announce else "open"
    rec = _insert(conn, channel_id, int(party["id"]), "ask", body.strip(),
                  title=title.strip(), slug=final, ref=ref, status=status)
    now = utc_now_iso()
    with writing(conn):
        for c in chosen:
            # The author has it in hand by definition, so their ack is stamped now.
            acked = now if int(c["id"]) == int(party["id"]) else None
            conn.execute(
                "INSERT INTO item_consumers (message_id, party_id, acked_at, prune_ok_at) "
                "VALUES (?, ?, ?, NULL)",
                (rec["id"], c["id"], acked),
            )
    _set_cursor(conn, channel_id, int(party["id"]), rec["seq"])
    return {
        "channel": ch["name"], "seq": rec["seq"], "slug": final, "status": status,
        "title": title.strip(),
        "consumers": [c["name"] for c in chosen],
        "awaiting_ack_from": [c["name"] for c in chosen if int(c["id"]) != int(party["id"])] if not announce else [],
    }


def _find_item(conn: sqlite3.Connection, party: dict, ref: str,
               channel: str | None = None, kinds: tuple[str, ...] = ("ask",)) -> dict:
    """Resolve 'slug', 'channel:seq' or '#seq'/'seq' to one message."""
    ref = (ref or "").strip()
    if not ref:
        raise ChatError("item is required (slug, or channel:seq)")

    channel_name, _, tail = ref.partition(":")
    if tail:
        ch = _channel_by_name(conn, channel_name)
        if ch is None:
            raise ChatError(f"no channel named '{channel_name}'")
        target = tail
    else:
        ch = resolve_channel(conn, party, channel)
        target = ref

    target = target.lstrip("#")
    if target.isdigit():
        row = conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND seq = ?", (ch["id"], int(target))
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND slug = ?", (ch["id"], target)
        ).fetchone()
    if row is None:
        raise ChatError(f"no message '{ref}' in channel '{ch['name']}'")
    if row["kind"] not in kinds:
        raise ChatError(f"'{ref}' is a {row['kind']}, expected one of: {', '.join(kinds)}")
    if not _is_seated(conn, int(row["channel_id"]), int(party["id"])):
        raise ChatError(f"you are not in channel '{ch['name']}'")
    return dict(row)


def reply(conn: sqlite3.Connection, party: dict, item: str, text: str,
          channel: str | None = None) -> dict:
    if not (text or "").strip():
        raise ChatError("text is required")
    parent = _find_item(conn, party, item, channel=channel, kinds=("ask", "say", "reply"))
    channel_id = int(parent["channel_id"])
    ch = _row(conn.execute("SELECT name FROM channels WHERE id = ?", (channel_id,)).fetchone())
    rec = _insert(conn, channel_id, int(party["id"]), "reply", text.strip(),
                  parent_id=int(parent["id"]))

    auto_acked = False
    if parent["kind"] == "ask" and parent["status"] == "open":
        # Replying to an open item is proof it is seen and in hand: stamp the ack
        # rather than leaving the item looking unattended.
        row = conn.execute(
            "SELECT acked_at FROM item_consumers WHERE message_id = ? AND party_id = ?",
            (parent["id"], party["id"]),
        ).fetchone()
        if row is not None and not row["acked_at"]:
            now = utc_now_iso()
            with writing(conn):
                conn.execute(
                    "UPDATE item_consumers SET acked_at = ? WHERE message_id = ? AND party_id = ?",
                    (now, parent["id"], party["id"]),
                )
                conn.execute(
                    "UPDATE messages SET status = 'ack', updated_at = ? WHERE id = ?",
                    (now, parent["id"]),
                )
            auto_acked = True

    _set_cursor(conn, channel_id, int(party["id"]), rec["seq"])
    return {
        "channel": ch["name"] if ch else "?", "seq": rec["seq"],
        "re": parent["slug"] or f"#{parent['seq']}",
        "auto_acked": auto_acked,
    }


def ack(conn: sqlite3.Connection, party: dict, item: str, note: str | None = None,
        channel: str | None = None) -> dict:
    msg = _find_item(conn, party, item, channel=channel)
    row = conn.execute(
        "SELECT acked_at FROM item_consumers WHERE message_id = ? AND party_id = ?",
        (msg["id"], party["id"]),
    ).fetchone()
    if row is None:
        raise ChatError(
            f"you are not a consumer of '{msg['slug']}' — only its consumers ack it"
        )
    already_acked = bool(row["acked_at"])
    now = utc_now_iso()
    with writing(conn):
        conn.execute(
            "UPDATE item_consumers SET acked_at = COALESCE(acked_at, ?) "
            "WHERE message_id = ? AND party_id = ?",
            (now, msg["id"], party["id"]),
        )
        if msg["status"] == "open":
            conn.execute("UPDATE messages SET status = 'ack', updated_at = ? WHERE id = ?",
                         (now, msg["id"]))
    if not already_acked:
        # A bare ack writes no prose, so without this the raiser has nothing to
        # notice — no channel line and no tick.
        _event(conn, int(msg["channel_id"]), party,
               f"[{msg['slug'] or msg['seq']}] acked by {party['name']}")
    if note:
        reply(conn, party, f"{_channel_name(conn, int(msg['channel_id']))}:{msg['seq']}", note)
    return _item_state(conn, int(msg["id"]))


def resolve_item(conn: sqlite3.Connection, party: dict, item: str, note: str | None = None,
                 answered: bool = False, channel: str | None = None) -> dict:
    msg = _find_item(conn, party, item, channel=channel)
    if msg["status"] == "done":
        return _item_state(conn, int(msg["id"]))

    is_author = int(msg["author_id"]) == int(party["id"])
    consumer = conn.execute(
        "SELECT acked_at FROM item_consumers WHERE message_id = ? AND party_id = ?",
        (msg["id"], party["id"]),
    ).fetchone()
    if not is_author:
        if consumer is None:
            raise ChatError(f"you are not a consumer of '{msg['slug']}'")
        if not consumer["acked_at"]:
            raise ChatError("ack it first (chat_ack), then resolve")
        if not answered:
            raise ChatError(
                "only the raiser confirms satisfaction. If you have fully answered it, "
                "call again with answered=true; otherwise reply and let them resolve."
            )

    now = utc_now_iso()
    with writing(conn):
        conn.execute("UPDATE messages SET status = 'done', updated_at = ? WHERE id = ?",
                     (now, msg["id"]))
    who = "raiser" if is_author else "answerer"
    _event(conn, int(msg["channel_id"]), party,
           f"[{msg['slug']}] resolved by {party['name']} ({who})")
    if note:
        reply(conn, party, f"{_channel_name(conn, int(msg['channel_id']))}:{msg['seq']}", note)
    return _item_state(conn, int(msg["id"]))


def prune_ok(conn: sqlite3.Connection, party: dict, item: str, channel: str | None = None) -> dict:
    msg = _find_item(conn, party, item, channel=channel)
    row = conn.execute(
        "SELECT prune_ok_at FROM item_consumers WHERE message_id = ? AND party_id = ?",
        (msg["id"], party["id"]),
    ).fetchone()
    if row is None:
        raise ChatError(f"you are not a consumer of '{msg['slug']}' — you cannot prune-ok it")
    with writing(conn):
        conn.execute(
            "UPDATE item_consumers SET prune_ok_at = COALESCE(prune_ok_at, ?) "
            "WHERE message_id = ? AND party_id = ?",
            (utc_now_iso(), msg["id"], party["id"]),
        )
    archived = sweep_archive(conn, party)
    state = _item_state(conn, int(msg["id"]))
    state["archived_now"] = msg["slug"] in archived
    return state


def _channel_name(conn: sqlite3.Connection, channel_id: int) -> str:
    row = conn.execute("SELECT name FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return row["name"] if row else "?"


def _item_state(conn: sqlite3.Connection, message_id: int) -> dict:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise ChatError("item vanished")
    state = _hydrate(conn, row)
    consumers = _consumer_state(conn, message_id)
    state["awaiting_ack_from"] = [c["party"] for c in consumers if not c["acked"]]
    state["awaiting_prune_ok_from"] = [c["party"] for c in consumers if not c["prune_ok"]]
    return state


def sweep_archive(conn: sqlite3.Connection, party: dict) -> list[str]:
    """Archive every DONE item whose consumers have all said prune-ok.

    Both gates are required, exactly as in the xsession protocol: resolved AND
    unanimous. Archiving is a flag, so nothing is ever lost.
    """
    rows = conn.execute(
        "SELECT m.id, m.slug, m.seq, m.channel_id FROM messages m "
        "WHERE m.kind = 'ask' AND m.status = 'done' AND m.is_archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM item_consumers ic "
        "                WHERE ic.message_id = m.id AND ic.prune_ok_at IS NULL)"
    ).fetchall()
    archived: list[str] = []
    for r in rows:
        with writing(conn):
            conn.execute(
                "UPDATE messages SET is_archived = 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), r["id"]),
            )
            # Replies belong to the thread and go with it.
            conn.execute(
                "UPDATE messages SET is_archived = 1 WHERE parent_id = ?", (r["id"],)
            )
        _event(conn, int(r["channel_id"]), party,
               f"[{r['slug'] or r['seq']}] archived (done + unanimous prune-ok)")
        archived.append(r["slug"] or str(r["seq"]))
    return archived


# ── Reading ──

def read(conn: sqlite3.Connection, party: dict, channel: str | None = None,
         limit: int | None = None, since: int | None = None, peek: bool = False,
         archived: bool = False) -> dict:
    ch = resolve_channel(conn, party, channel)
    channel_id = int(ch["id"])
    swept = sweep_archive(conn, party)

    cfg_limit = int(get_config().get("chat", {}).get("read_limit", 50) or 50)
    limit = int(limit or cfg_limit)
    start = int(since) if since is not None else _cursor_seq(conn, channel_id, int(party["id"]))

    rows = conn.execute(
        f"SELECT * FROM messages WHERE channel_id = ? AND seq > ? AND is_archived = {1 if archived else 0} "
        "ORDER BY seq LIMIT ?",
        (channel_id, start, limit),
    ).fetchall()
    messages = [_hydrate(conn, r) for r in rows]

    highest = max((int(r["seq"]) for r in rows), default=start)
    remaining = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM messages WHERE channel_id = ? AND seq > ? "
        f"AND is_archived = {1 if archived else 0}",
        (channel_id, highest),
    ).fetchone()["n"])

    if not peek and not archived:
        _set_cursor(conn, channel_id, int(party["id"]), highest)

    return {
        "channel": ch["name"],
        "from_seq": start,
        "messages": messages,
        "more": remaining,
        "cursor": highest if not peek and not archived else start,
        "archived_by_this_read": swept,
        "parties": [p["name"] for p in channel_parties(conn, channel_id)],
    }


def inbox(conn: sqlite3.Connection, party: dict) -> dict:
    """What waits on ME, across every channel — the one call worth making often."""
    sweep_archive(conn, party)
    pid = int(party["id"])
    channels_out: list[dict] = []
    total_unread = 0
    needs_me: list[dict] = []
    needs_peer: list[dict] = []

    for ch in my_channels(conn, pid):
        channel_id = int(ch["id"])
        last_seq = _cursor_seq(conn, channel_id, pid)
        unread = int(conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE channel_id = ? AND seq > ? "
            "AND is_archived = 0 AND author_id != ?",
            (channel_id, last_seq, pid),
        ).fetchone()["n"])
        total_unread += unread
        channels_out.append({
            "channel": ch["name"],
            "unread": unread,
            "parties": [p["name"] for p in channel_parties(conn, channel_id)],
        })

        items = conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND kind = 'ask' AND is_archived = 0 "
            "ORDER BY seq",
            (channel_id,),
        ).fetchall()
        for it in items:
            consumers = _consumer_state(conn, int(it["id"]))
            mine = conn.execute(
                "SELECT acked_at, prune_ok_at FROM item_consumers "
                "WHERE message_id = ? AND party_id = ?",
                (it["id"], pid),
            ).fetchone()
            is_author = int(it["author_id"]) == pid
            entry = {
                "channel": ch["name"],
                "item": it["slug"] or f"#{it['seq']}",
                "seq": it["seq"],
                "title": it["title"],
                "status": it["status"],
                "from": _party_name(conn, int(it["author_id"])),
                "mine": is_author,
            }
            if mine is not None and it["status"] in ("open", "ack") and not mine["acked_at"]:
                needs_me.append({**entry, "needs": "your ack (chat_ack) — it is unattended"})
            elif is_author and it["status"] == "ack":
                needs_me.append({**entry, "needs": "your resolve (chat_resolve) once satisfied"})
            elif mine is not None and it["status"] == "done" and not mine["prune_ok_at"]:
                needs_me.append({**entry, "needs": "your prune-ok (chat_prune_ok)"})
            else:
                waiting = [c["party"] for c in consumers
                           if (it["status"] != "done" and not c["acked"])
                           or (it["status"] == "done" and not c["prune_ok"])]
                if it["status"] == "ack" and not waiting:
                    waiting = [_party_name(conn, int(it["author_id"]))]
                needs_peer.append({**entry, "waiting_on": waiting})

    return {
        "me": party["name"],
        "workspace": party["workspace_path"],
        "unread_total": total_unread,
        "channels": channels_out,
        "waiting_on_me": needs_me,
        "waiting_on_peer": needs_peer,
        "live": bool(channels_out),
    }


def _party_name(conn: sqlite3.Connection, party_id: int) -> str:
    row = conn.execute("SELECT name FROM parties WHERE id = ?", (party_id,)).fetchone()
    return row["name"] if row else "?"


def pending_count(conn: sqlite3.Connection, party: dict, channel: str | None = None) -> int:
    """Cheap 'is there anything for me' probe, used by chat_wait."""
    pid = int(party["id"])
    channels = [resolve_channel(conn, party, channel)] if channel else my_channels(conn, pid)
    total = 0
    for ch in channels:
        channel_id = int(ch["id"])
        last_seq = _cursor_seq(conn, channel_id, pid)
        total += int(conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE channel_id = ? AND seq > ? "
            "AND is_archived = 0 AND author_id != ?",
            (channel_id, last_seq, pid),
        ).fetchone()["n"])
        total += int(conn.execute(
            "SELECT COUNT(*) AS n FROM messages m JOIN item_consumers ic ON ic.message_id = m.id "
            "WHERE m.channel_id = ? AND m.is_archived = 0 AND ic.party_id = ? "
            "AND ((m.status IN ('open','ack') AND ic.acked_at IS NULL) "
            "     OR (m.status = 'done' AND ic.prune_ok_at IS NULL))",
            (channel_id, pid),
        ).fetchone()["n"])
    return total


def whoami(conn: sqlite3.Connection, party: dict) -> dict:
    box = inbox(conn, party)
    return {
        "name": party["name"],
        "workspace": party["workspace_path"],
        "about": party.get("about"),
        "os_user": party.get("os_user"),
        "channels": box["channels"],
        "unread_total": box["unread_total"],
        "waiting_on_me": len(box["waiting_on_me"]),
        "live": box["live"],
        "known_parties": [
            {"name": r["name"], "workspace": r["workspace_path"], "about": r["about"]}
            for r in conn.execute(
                "SELECT name, workspace_path, about FROM parties ORDER BY name"
            ).fetchall()
        ],
    }
