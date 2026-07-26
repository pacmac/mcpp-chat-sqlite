"""Tool-surface tests: the execute() contract as the host calls it."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import MODULE_DIR, call


def _manifest() -> dict:
    return yaml.safe_load((MODULE_DIR / "tool.yaml").read_text(encoding="utf-8"))


def test_manifest_matches_handlers(tool):
    names = [t["name"] for t in _manifest()["tools"]]
    assert sorted(names) == sorted(tool._HANDLERS)
    assert len(names) == 14


def test_manifest_is_wellformed_for_the_host():
    manifest = _manifest()
    assert manifest["name"] == "chat"
    assert manifest["scope"] == "local"
    assert manifest["about"].strip()
    for t in manifest["tools"]:
        assert t["name"].startswith("chat_"), t["name"]
        assert t["description"].strip()
        assert t["inputSchema"]["type"] == "object"


def test_no_tool_is_named_help():
    assert "help" not in {t["name"] for t in _manifest()["tools"]}


def test_unknown_tool_is_reported(tool, repos):
    a, _b = repos
    out = call(tool, "chat_nope", {}, a)
    assert out["success"] is False and "Unknown tool" in out["error"]


def test_whoami_creates_the_party(tool, repos):
    a, _b = repos
    out = call(tool, "chat_whoami", {}, a)
    assert out["success"] is True
    assert out["result"]["name"] == "mesh-gw"
    assert out["result"]["workspace"] == str(a)
    assert "mesh-gw" in out["display"]


def test_whoami_sets_about(tool, repos):
    a, _b = repos
    out = call(tool, "chat_whoami", {"about": "BLE gateway"}, a)
    assert out["result"]["about"] == "BLE gateway"


def test_errors_come_back_as_failures_not_exceptions(tool, repos):
    a, _b = repos
    out = call(tool, "chat_say", {"text": "hi"}, a)
    assert out["success"] is False
    assert "not in any channel" in out["error"]


def test_every_success_carries_display_text(tool, repos):
    a, b = repos
    call(tool, "chat_whoami", {}, b)
    outs = [
        call(tool, "chat_channel_new", {"name": "link", "parties": ["node-dash"]}, a),
        call(tool, "chat_say", {"text": "hello"}, a),
        call(tool, "chat_channel_list", {}, a),
        call(tool, "chat_inbox", {}, b),
        call(tool, "chat_read", {}, b),
    ]
    for out in outs:
        assert out["success"] is True, out
        assert isinstance(out["display"], str) and out["display"].strip()


def test_full_two_repo_handshake_through_execute(tool, repos):
    """The worked example from the spec, driven exactly as two sessions would."""
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)                       # dash announces itself
    call(tool, "chat_channel_new", {"name": "gw--dash", "parties": ["node-dash"]}, gw)

    asked = call(tool, "chat_ask", {
        "title": "Is the chunk header enum closed?",
        "body": "I need a new frame type; can your generator emit one?",
        "slug": "chunk-api",
    }, gw)
    assert asked["success"] and asked["result"]["status"] == "open"

    box = call(tool, "chat_inbox", {}, dash)
    assert box["result"]["waiting_on_me"][0]["item"] == "chunk-api"
    assert "Waiting on YOU" in box["display"]

    replied = call(tool, "chat_reply", {
        "item": "chunk-api", "text": "I own the generator — adding FRAME_AUX.",
    }, dash)
    assert replied["success"] and replied["result"]["auto_acked"] is True

    read = call(tool, "chat_read", {}, gw)
    assert "FRAME_AUX" in read["display"]

    resolved = call(tool, "chat_resolve", {"item": "chunk-api", "note": "Confirmed."}, gw)
    assert resolved["result"]["status"] == "done"

    assert call(tool, "chat_prune_ok", {"item": "chunk-api"}, gw)["result"]["archived_now"] is False
    final = call(tool, "chat_prune_ok", {"item": "chunk-api"}, dash)
    assert final["result"]["archived_now"] is True

    after = call(tool, "chat_read", {"since": 0, "peek": True}, gw)
    assert "chunk-api" not in [m.get("slug") for m in after["result"]["messages"]]
    history = call(tool, "chat_read", {"since": 0, "peek": True, "archived": True}, gw)
    assert "chunk-api" in [m.get("slug") for m in history["result"]["messages"]]


def test_item_can_be_addressed_as_channel_colon_seq(tool, repos):
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["node-dash"]}, gw)
    asked = call(tool, "chat_ask", {"title": "q", "body": "b"}, gw)
    seq = asked["result"]["seq"]
    out = call(tool, "chat_ack", {"item": f"link:{seq}"}, dash)
    assert out["success"] and out["result"]["status"] == "ack"


def test_join_and_leave(tool, repos, tmp_path):
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": []}, gw)
    joined = call(tool, "chat_channel_join", {"name": "link"}, dash)
    assert set(joined["result"]["parties"]) == {"mesh-gw", "node-dash"}
    left = call(tool, "chat_channel_leave", {"name": "link"}, dash)
    assert left["success"] and left["result"]["left"] is True


def test_wait_returns_immediately_when_work_is_pending(tool, repos):
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["node-dash"]}, gw)
    call(tool, "chat_ask", {"title": "q", "body": "b", "slug": "q"}, gw)
    out = call(tool, "chat_wait", {"seconds": 20}, dash)
    assert out["success"] and out["result"]["arrived"] is True
    assert "Something arrived" in out["display"]


def test_wait_is_bounded_when_nothing_arrives(tool, repos):
    import time
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["node-dash"]}, gw)
    start = time.monotonic()
    out = call(tool, "chat_wait", {"seconds": 1}, dash)
    elapsed = time.monotonic() - start
    assert out["success"] and out["result"]["arrived"] is False
    assert 1 <= elapsed < 5      # honours the bound, nowhere near the host's 30s cap


def test_wait_is_clamped_to_the_host_budget(tool, repos, monkeypatch):
    monkeypatch.setenv("MCPP_TIMEOUT_SECONDS", "6")
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["node-dash"]}, gw)
    call(tool, "chat_ask", {"title": "q", "body": "b"}, gw)   # so it returns at once
    out = call(tool, "chat_wait", {"seconds": 20}, dash)
    assert out["result"]["waited_seconds"] <= 1


def test_get_info_reports_identity_and_boundary(tool, repos):
    a, _b = repos
    info = tool.get_info({"workspace_dir": str(a), "module_dir": str(MODULE_DIR),
                          "module_scope": "local"})
    assert info["identity"]["workspace_dir"] == str(a)
    assert info["params"]["read_limit"]["default"] == 50
    assert any("authoritative" in line for line in info["boundary"])
    assert any("prune-ok" in line for line in info["protocol"])


def test_initialize_reports_success(tool):
    assert tool.initialize() == {"success": True}



# ── Always live ──

def test_wait_always_points_a_seated_session_back_at_itself(tool, repos, channel, monkeypatch, tmp_path):
    cfg = tmp_path / "live.yaml"
    cfg.write_text("chat:\n  always_live: true\n", encoding="utf-8")
    monkeypatch.setenv("MCPP_CHAT_CONFIG", str(cfg))
    gw, _dash = repos
    out = call(tool, "chat_wait", {"seconds": 1}, gw)
    assert out["result"]["arrived"] is False
    assert out["result"]["next"] == "chat_wait", "the loop must never dead-end"
    assert out["result"]["live"] is True


def test_wait_does_not_loop_a_repo_that_is_on_no_channel(tool, repos):
    gw, _dash = repos
    call(tool, "chat_whoami", {}, gw)
    out = call(tool, "chat_wait", {"seconds": 1}, gw)
    assert "next" not in out["result"]
    assert out["result"]["live"] is False
