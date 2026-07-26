"""Hook tests: what a session is told, and the guarantees around failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import MODULE_DIR, call

HOOK = MODULE_DIR / "hook.py"


def run_hook(event: str, cwd: Path, db: Path, payload: dict | None = None) -> tuple[int, str]:
    body = dict(payload or {})
    body.setdefault("cwd", str(cwd))
    env = {**os.environ, "MCPP_CHAT_DB": str(db)}
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--event", event],
        input=json.dumps(body), capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout.strip()


@pytest.fixture()
def chatting(tool, repos, tmp_path):
    """node-dash has asked mesh-gw a question and is waiting."""
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)          # both repos must exist as parties
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "gw--dash", "parties": ["mesh-gw"]}, dash)
    call(tool, "chat_ask", {
        "title": "Is the chunk header enum closed?",
        "body": "I need FRAME_AUX; can your generator emit it?",
        "slug": "chunk-api",
    }, dash)
    return gw, dash, tmp_path / "chat.db"


def test_silent_when_repo_never_chatted(tool, tmp_path, repos):
    gw, _dash = repos
    call(tool, "chat_whoami", {}, gw)          # db exists, but no channels
    stranger = tmp_path / "unrelated-repo"
    stranger.mkdir()
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        rc, out = run_hook(event, stranger, tmp_path / "chat.db")
        assert rc == 0 and out == "", f"{event} should be silent"


def test_silent_when_nothing_pending(tool, repos, tmp_path):
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)
    rc, out = run_hook("Stop", gw, tmp_path / "chat.db")
    assert rc == 0 and out == ""


def test_missing_database_is_silent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, out = run_hook("Stop", repo, tmp_path / "nonexistent.db")
    assert rc == 0 and out == ""


def test_corrupt_database_is_silent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database", encoding="utf-8")
    rc, out = run_hook("Stop", repo, junk)
    assert rc == 0 and out == ""


def test_malformed_stdin_is_silent(tmp_path):
    env = {**os.environ, "MCPP_CHAT_DB": str(tmp_path / "chat.db")}
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--event", "Stop"],
        input="{not json at all", capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_session_start_reports_pending_work(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("SessionStart", gw, db)
    assert rc == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "mesh-gw" in ctx
    assert "chunk-api" in ctx and "needs your ack" in ctx
    assert "FRAME_AUX" in ctx           # the peer's actual words, not just a count


def test_user_prompt_submit_uses_its_own_event_name(chatting):
    gw, _dash, db = chatting
    _rc, out = run_hook("UserPromptSubmit", gw, db)
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


@pytest.fixture()
def not_live(tmp_path, monkeypatch):
    """Disable always-live, leaving only the older 'a peer is stuck' nag rule.

    That rule still exists and still has to be right — it is what runs when a
    host turns staying-live off — so its tests keep running against it.
    """
    cfg = tmp_path / "quiet.yaml"
    cfg.write_text("chat:\n  always_live: false\n", encoding="utf-8")
    monkeypatch.setenv("MCPP_CHAT_CONFIG", str(cfg))
    return cfg


def test_stop_blocks_and_says_what_to_do(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("Stop", gw, db)
    assert rc == 0
    data = json.loads(out)
    assert data["decision"] == "block"
    assert "chunk-api" in data["reason"]
    assert "chat_wait" in data["reason"]
    assert "own repo" in data["reason"]


def test_stop_does_not_block_twice_when_not_staying_live(chatting, not_live):
    gw, _dash, db = chatting
    rc, out = run_hook("Stop", gw, db, payload={"stop_hook_active": True})
    assert rc == 0 and out == ""


def test_hook_never_advances_the_cursor(tool, chatting):
    """Showing a message must not consume it — the agent still has to read it."""
    gw, _dash, db = chatting
    run_hook("SessionStart", gw, db)
    run_hook("Stop", gw, db)
    read = call(tool, "chat_read", {}, gw)
    assert any(m.get("slug") == "chunk-api" for m in read["result"]["messages"])


def test_hook_writes_nothing_at_all(tool, chatting):
    gw, _dash, db = chatting
    before = db.read_bytes()
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        run_hook(event, gw, db)
    assert db.read_bytes() == before


def test_author_is_not_told_to_ack_their_own_item(chatting, not_live):
    _gw, dash, db = chatting
    rc, out = run_hook("Stop", dash, db)
    # node-dash raised it, so nothing is waiting on node-dash yet.
    assert rc == 0 and out == ""


def test_prune_ok_is_reported_but_does_not_block(tool, chatting, not_live):
    """Housekeeping shows up in context; it must not hold a turn open."""
    gw, dash, db = chatting
    call(tool, "chat_ack", {"item": "chunk-api"}, gw)
    call(tool, "chat_reply", {"item": "chunk-api", "text": "no, it is open"}, gw)
    call(tool, "chat_resolve", {"item": "chunk-api"}, dash)
    call(tool, "chat_read", {}, gw)                     # clear unread chatter

    rc, stop_out = run_hook("Stop", gw, db)
    assert rc == 0 and stop_out == ""                   # no nag

    _rc, start_out = run_hook("SessionStart", gw, db)
    ctx = json.loads(start_out)["hookSpecificOutput"]["additionalContext"]
    assert "prune-ok" in ctx                            # still visible


def test_parked_item_awaiting_my_resolve_never_blocks(tool, chatting, not_live):
    """The reported bug: an answered item held open on purpose must not nag.

    node-dash raised it, mesh-gw answered, and node-dash is deliberately waiting
    for mesh-gw to ship before resolving. That is a correct board state.
    """
    gw, dash, db = chatting
    call(tool, "chat_ack", {"item": "chunk-api"}, gw)
    call(tool, "chat_reply", {"item": "chunk-api", "text": "fix is queued, not shipped"}, gw)
    call(tool, "chat_read", {}, dash)                   # raiser has read the answer

    for _ in range(3):                                  # would nag every turn before
        rc, out = run_hook("Stop", dash, db)
        assert rc == 0 and out == ""

    _rc, start_out = run_hook("SessionStart", dash, db)
    ctx = json.loads(start_out)["hookSpecificOutput"]["additionalContext"]
    assert "resolve when you are satisfied" in ctx


def test_event_only_unread_does_not_block(tool, chatting, not_live):
    """A peer's ack is news; it must not cost the raiser a turn."""
    gw, dash, db = chatting
    call(tool, "chat_read", {}, dash)
    call(tool, "chat_ack", {"item": "chunk-api"}, gw)   # writes an event row only

    rc, out = run_hook("Stop", dash, db)
    assert rc == 0 and out == ""

    _rc, start_out = run_hook("SessionStart", dash, db)
    assert "acked by mesh-gw" in json.loads(start_out)["hookSpecificOutput"]["additionalContext"]


def test_real_message_still_blocks(tool, chatting, not_live):
    """The narrowing must not silence an actual question."""
    gw, dash, db = chatting
    call(tool, "chat_read", {}, dash)
    call(tool, "chat_ack", {"item": "chunk-api"}, gw)
    call(tool, "chat_say", {"text": "what MTU are you assuming?", "channel": "gw--dash"}, gw)

    rc, out = run_hook("Stop", dash, db)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    assert "MTU" in json.loads(out)["reason"]


def test_unknown_event_prints_plain_text(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("SomethingElse", gw, db)
    assert rc == 0 and "chunk-api" in out and not out.startswith("{")


# ── Always live ──
#
# Being on a channel is not a mode. A seated session must never go quiet, or a
# peer writing a minute later reaches nobody and a human ends up relaying again.

def test_seated_session_is_not_allowed_to_go_quiet(tool, repos, tmp_path):
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)

    rc, out = run_hook("Stop", gw, tmp_path / "chat.db")
    assert rc == 0
    payload = json.loads(out)
    assert payload["decision"] == "block", "an empty inbox is exactly when it must wait"
    assert "chat_wait" in payload["reason"]


def test_staying_live_survives_stop_hook_active(tool, repos, tmp_path):
    """The fire-once guard must not retire the loop after a single block."""
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)

    for round_no in range(3):
        rc, out = run_hook("Stop", gw, tmp_path / "chat.db", {"stop_hook_active": True})
        assert rc == 0 and out, f"went quiet on round {round_no}"
        assert json.loads(out)["decision"] == "block"


def test_leaving_the_channel_is_the_off_switch(tool, repos, tmp_path):
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)
    call(tool, "chat_channel_leave", {"name": "link"}, gw)

    rc, out = run_hook("Stop", gw, tmp_path / "chat.db")
    assert rc == 0 and out == "", "a repo that has left must be able to finish"


def test_a_repo_on_no_channel_is_unaffected(tool, tmp_path, repos):
    gw, _dash = repos
    call(tool, "chat_whoami", {}, gw)          # a party, but seated nowhere
    rc, out = run_hook("Stop", gw, tmp_path / "chat.db")
    assert rc == 0 and out == ""


def test_new_session_joins_the_conversation_unprompted(tool, repos, tmp_path):
    """SessionStart must arm the loop, or every session needs a human first."""
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)

    rc, out = run_hook("SessionStart", gw, tmp_path / "chat.db")
    assert rc == 0 and out
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "chat_wait" in ctx


def test_host_wide_off_switch(tool, repos, tmp_path, monkeypatch):
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)

    cfg = tmp_path / "off.yaml"
    cfg.write_text("chat:\n  always_live: false\n", encoding="utf-8")
    monkeypatch.setenv("MCPP_CHAT_CONFIG", str(cfg))
    rc, out = run_hook("Stop", gw, tmp_path / "chat.db")
    assert rc == 0 and out == ""
