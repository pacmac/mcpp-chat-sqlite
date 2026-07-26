"""MCP entry point for mcpp-chat.

Loaded by mcpp via spec_from_file_location; exports execute(), get_info() and
initialize(). Sibling modules are imported by bootstrapping a real package in
sys.modules first, so their relative imports work both here and under pytest.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

_PKG_DIR = Path(__file__).resolve().parent
_PKG_NAME = "mcpp_chat"
_pkg_cache: tuple[Any, Any, Any] | None = None


def _pkg() -> tuple[Any, Any, Any]:
    """Import (config, db, chat) as submodules of a real package. Cached."""
    global _pkg_cache
    if _pkg_cache is not None:
        return _pkg_cache

    import importlib.util

    if _PKG_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PKG_NAME, _PKG_DIR / "__init__.py",
            submodule_search_locations=[str(_PKG_DIR)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot bootstrap package at {_PKG_DIR}")
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_PKG_NAME] = pkg
        spec.loader.exec_module(pkg)

    loaded = []
    for name in ("config", "db", "chat"):
        full = f"{_PKG_NAME}.{name}"
        if full in sys.modules:
            loaded.append(sys.modules[full])
            continue
        spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {full}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        loaded.append(mod)

    _pkg_cache = (loaded[0], loaded[1], loaded[2])
    return _pkg_cache


# ── Display formatters (audience: user) ──

def _fmt_whoami(data: dict) -> str:
    lines = [f"**{data['name']}** — {data['workspace']}"]
    if data.get("about"):
        lines.append(f"_{data['about']}_")
    if data["channels"]:
        for c in data["channels"]:
            peers = ", ".join(p for p in c["parties"] if p != data["name"]) or "no peers yet"
            unread = f" — {c['unread']} unread" if c["unread"] else ""
            lines.append(f"- `{c['channel']}` with {peers}{unread}")
    else:
        lines.append("- in no channels yet (chat_channel_new to start one)")
    if data["waiting_on_me"]:
        lines.append(f"\n**{data['waiting_on_me']} item(s) wait on you** — chat_inbox for detail")
    others = [p for p in data["known_parties"] if p["name"] != data["name"]]
    if others:
        lines.append("\n**Known parties**")
        for p in others:
            about = f" — {p['about']}" if p.get("about") else ""
            lines.append(f"- `{p['name']}` ({p['workspace']}){about}")
    return "\n".join(lines)


def _fmt_channels(rows: list[dict], me: str) -> str:
    if not rows:
        return "No channels. Start one with chat_channel_new."
    lines = ["**Channels**"]
    for c in rows:
        peers = ", ".join(p for p in c["parties"] if p != me) or "—"
        flags = []
        if c.get("unread"):
            flags.append(f"{c['unread']} unread")
        if c.get("live_items"):
            flags.append(f"{c['live_items']} live item(s)")
        if not c.get("member", True):
            flags.append("not a member")
        if c.get("closed"):
            flags.append("closed")
        tail = f" — {'; '.join(flags)}" if flags else ""
        about = f"\n  _{c['about']}_" if c.get("about") else ""
        lines.append(f"- `{c['channel']}` with {peers}{tail}{about}")
    return "\n".join(lines)


def _fmt_message(m: dict) -> str:
    if m["kind"] == "event":
        return f"  · _{m['body']}_"
    head = f"**{m['from']}** #{m['seq']}"
    if m["kind"] == "ask":
        status = m.get("status", "?").upper()
        pending = [c["party"] for c in m.get("consumers", []) if not c["acked"]]
        wait = f" — awaiting ack: {', '.join(pending)}" if pending and status != "DONE" else ""
        head += f" · [{m.get('slug', '?')}] **{m.get('title', '')}** · {status}{wait}"
    elif m.get("re"):
        head += f" · re [{m['re']}]"
    ref = f"\n  ref: `{m['ref']}`" if m.get("ref") else ""
    body = "\n".join(f"  {line}" for line in m["body"].splitlines())
    return f"{head}\n{body}{ref}"


def _fmt_read(data: dict) -> str:
    header = f"**{data['channel']}** ({', '.join(data['parties'])})"
    if not data["messages"]:
        return f"{header}\nNothing new."
    parts = [header] + [_fmt_message(m) for m in data["messages"]]
    if data.get("more"):
        parts.append(f"_… {data['more']} more, call chat_read again_")
    if data.get("archived_by_this_read"):
        parts.append(f"_archived: {', '.join(data['archived_by_this_read'])}_")
    return "\n\n".join(parts)


def _fmt_inbox(data: dict, header: str | None = None) -> str:
    lines = [header] if header else []
    lines.append(f"**Inbox — {data['me']}** · {data['unread_total']} unread")
    if data["waiting_on_me"]:
        lines.append("\n**Waiting on YOU**")
        for it in data["waiting_on_me"]:
            lines.append(
                f"- `{it['channel']}` [{it['item']}] *{it['title']}* — from {it['from']} — {it['needs']}"
            )
    else:
        lines.append("\nNothing waiting on you.")
    if data["waiting_on_peer"]:
        lines.append("\n**Waiting on a peer**")
        for it in data["waiting_on_peer"]:
            who = ", ".join(it.get("waiting_on") or []) or "—"
            lines.append(
                f"- `{it['channel']}` [{it['item']}] *{it['title']}* — {it['status'].upper()} — waiting on {who}"
            )
    unread_ch = [c for c in data["channels"] if c["unread"]]
    if unread_ch:
        lines.append("\n**Unread**")
        for c in unread_ch:
            lines.append(f"- `{c['channel']}`: {c['unread']} — chat_read to see them")
    return "\n".join(lines)


def _fmt_item(data: dict, verb: str) -> str:
    lines = [
        f"**[{data.get('slug', '?')}]** {data.get('title', '')} — {verb} · "
        f"{str(data.get('status', '')).upper()} in `{data['channel']}`"
    ]
    if data.get("awaiting_ack_from"):
        lines.append(f"- awaiting ack from: {', '.join(data['awaiting_ack_from'])}")
    if data.get("status") == "done":
        pending = data.get("awaiting_prune_ok_from") or []
        if pending:
            lines.append(f"- awaiting prune-ok from: {', '.join(pending)}")
        elif data.get("archived_now"):
            lines.append("- archived: done with unanimous prune-ok")
    return "\n".join(lines)


# ── Handlers ──

def _h_whoami(chat, conn, party, args):
    if args.get("about"):
        party = chat.set_about(conn, party, str(args["about"]).strip())
    data = chat.whoami(conn, party)
    return data, _fmt_whoami(data)


def _h_channel_new(chat, conn, party, args):
    data = chat.create_channel(
        conn, party, args.get("name", ""), args.get("about"), args.get("parties") or []
    )
    peers = ", ".join(p for p in data["parties"] if p != party["name"]) or "nobody yet"
    return data, f"Created `{data['channel']}` with {peers}."


def _h_channel_list(chat, conn, party, args):
    rows = chat.list_channels(conn, party, bool(args.get("all")))
    return {"channels": rows}, _fmt_channels(rows, party["name"])


def _h_channel_join(chat, conn, party, args):
    data = chat.join_channel(conn, party, args.get("name", ""))
    return data, f"Joined `{data['channel']}` ({', '.join(data['parties'])})."


def _h_channel_leave(chat, conn, party, args):
    data = chat.leave_channel(conn, party, args.get("name", ""))
    return data, f"Left `{data['channel']}`."


def _h_say(chat, conn, party, args):
    data = chat.say(conn, party, args.get("text", ""), args.get("channel"),
                    args.get("reply_to"), args.get("ref"))
    if "re" in data:
        return data, f"Replied in `{data['channel']}` #{data['seq']} (re [{data['re']}])."
    to = ", ".join(data.get("to") or []) or "nobody yet"
    return data, f"Posted to `{data['channel']}` #{data['seq']} → {to}."


def _h_ask(chat, conn, party, args):
    data = chat.ask(
        conn, party, args.get("title", ""), args.get("body", ""), args.get("channel"),
        args.get("slug"), args.get("ref"), args.get("consumers"), bool(args.get("announce")),
    )
    waiting = ", ".join(data.get("awaiting_ack_from") or []) or "—"
    display = (
        f"**[{data['slug']}]** {data['title']} — {data['status'].upper()} in `{data['channel']}` "
        f"#{data['seq']}\n- consumers: {', '.join(data['consumers'])}\n- awaiting ack: {waiting}"
    )
    return data, display


def _h_reply(chat, conn, party, args):
    data = chat.reply(conn, party, args.get("item", ""), args.get("text", ""))
    extra = " (auto-acked)" if data.get("auto_acked") else ""
    return data, f"Replied in `{data['channel']}` #{data['seq']} re [{data['re']}]{extra}."


def _h_ack(chat, conn, party, args):
    data = chat.ack(conn, party, args.get("item", ""), args.get("note"))
    return data, _fmt_item(data, "acked")


def _h_resolve(chat, conn, party, args):
    data = chat.resolve_item(conn, party, args.get("item", ""), args.get("note"),
                             bool(args.get("answered")))
    return data, _fmt_item(data, "resolved")


def _h_prune_ok(chat, conn, party, args):
    data = chat.prune_ok(conn, party, args.get("item", ""))
    return data, _fmt_item(data, "prune-ok recorded")


def _h_read(chat, conn, party, args):
    data = chat.read(conn, party, args.get("channel"), args.get("limit"), args.get("since"),
                     bool(args.get("peek")), bool(args.get("archived")))
    return data, _fmt_read(data)


def _h_inbox(chat, conn, party, args):
    data = chat.inbox(conn, party)
    return data, _fmt_inbox(data)


def _h_wait(chat, conn, party, args):
    config, _db, _chat = _pkg()
    cfg = config.get_config().get("chat", {})
    hard_max = int(cfg.get("wait_max_seconds", 20) or 20)
    # Stay clear of the host's per-call SIGALRM budget. This, not the config
    # value, is what really bounds a poll — a long-poll cannot outlive the call.
    try:
        host_budget = int(os.environ.get("MCPP_TIMEOUT_SECONDS", "30")) - 5
    except ValueError:
        host_budget = 25
    limit = max(1, min(hard_max, host_budget))
    seconds = max(1, min(int(args.get("seconds") or 10), limit))
    channel = args.get("channel")

    deadline = time.monotonic() + seconds
    arrived = chat.pending_count(conn, party, channel) > 0
    while not arrived and time.monotonic() < deadline:
        time.sleep(0.5)
        arrived = chat.pending_count(conn, party, channel) > 0

    data = chat.inbox(conn, party)
    data["waited_seconds"] = round(seconds if not arrived else max(0.0, seconds - (deadline - time.monotonic())), 1)
    data["arrived"] = arrived

    header = "**Something arrived.**" if arrived else f"**Nothing arrived in {seconds}s.**"

    if chat.is_live(conn, party):
        # Never leave a seated session with nothing to do next, or its turn ends
        # and the repo stops being reachable until a human types.
        data["next"] = "chat_wait"
        header += (
            "\n**Still on the channel.** Answer anything above that is addressed to you, "
            "then call `chat_wait` again."
        )
    return data, _fmt_inbox(data, header)


_HANDLERS: dict[str, Callable] = {
    "chat_whoami": _h_whoami,
    "chat_channel_new": _h_channel_new,
    "chat_channel_list": _h_channel_list,
    "chat_channel_join": _h_channel_join,
    "chat_channel_leave": _h_channel_leave,
    "chat_say": _h_say,
    "chat_ask": _h_ask,
    "chat_reply": _h_reply,
    "chat_ack": _h_ack,
    "chat_resolve": _h_resolve,
    "chat_prune_ok": _h_prune_ok,
    "chat_read": _h_read,
    "chat_inbox": _h_inbox,
    "chat_wait": _h_wait,
}


# ── mcpp contract ──

def initialize() -> dict[str, Any]:
    try:
        _config, db, _chat = _pkg()
        conn = db.open_db()
        conn.close()
        return {"success": True}
    except Exception as exc:  # pragma: no cover - reported by the host as a warning
        return {"success": False, "message": f"mcpp-chat init failed: {exc}"}


def get_info(context: dict[str, Any] | None = None) -> dict[str, Any]:
    config, db, chat = _pkg()
    cfg = config.get_config()
    workspace = (context or {}).get("workspace_dir") or os.getcwd()
    info: dict[str, Any] = {
        "about": "Cross-session chat between agent sessions in different repos.",
        "identity": {
            "party_name": str(cfg.get("identity", {}).get("name") or Path(workspace).name),
            "workspace_dir": workspace,
            "note": "A party is a repository, not a person. Identity comes from the session's cwd.",
        },
        "db_path": str(db.resolve_db_path()),
        "tick_file": str(db.tick_path() or ""),
        "params": {
            "read_limit": {"values": "1-500", "default": cfg["chat"]["read_limit"]},
            "wait_seconds": {"values": "1-120 (the host's per-call budget may shorten it)",
                             "default": 10},
            "default_channel": {"values": None, "default": cfg["chat"]["default_channel"] or None},
        },
        "protocol": [
            "say/reply = chatter, consumed by reading.",
            "ask = item: OPEN -> ACK -> DONE, archived only when DONE and every consumer has prune-ok'd.",
            "Only the raiser confirms satisfaction; an answerer resolves with answered=true.",
            "chat_prune_ok adds only your own name — never speak for another party.",
        ],
        "boundary": [
            "You are authoritative about your own repo and nothing else.",
            "Never assert what a peer's code can or cannot do — ask.",
            "Never edit another party's files; raise an item instead.",
        ],
    }
    return info


def execute(tool_name: str, arguments: dict[str, Any],
            context: dict[str, Any] | None = None) -> dict[str, Any]:
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    try:
        _config, db, chat = _pkg()
    except Exception as exc:
        return {"success": False, "error": f"mcpp-chat failed to load: {exc}"}

    workspace = (context or {}).get("workspace_dir") or os.getcwd()
    conn = None
    try:
        conn = db.open_db()
        party = chat.party_for(conn, workspace)
        result, display = handler(chat, conn, party, arguments or {})
        return {"success": True, "result": result, "display": display}
    except chat.ChatError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if conn is not None:
            conn.close()
