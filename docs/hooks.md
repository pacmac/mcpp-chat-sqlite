---
title: Making a session notice chat without being told
status: draft
task: mcpp-chat-hooks (mcpp-plan context 721)
date: 2026-07-25
---

# Automatic surfacing via hooks

> **Superseded in part by [listen.md](listen.md).** What is below still describes
> how a session is *told* about waiting work. It does not make a session *act* —
> that needs the listen loop, and the `Stop` rules here are overridden while a
> party is listening.

## The problem

The `chat_*` tools only run when the agent decides to call one. An idle session
has no reason to, so in practice a human ends up prompting each session to go
read its messages — which is exactly the relaying the channel was built to
remove.

Nothing inside the module can fix this. MCP is request/response: a peer's write
cannot interrupt another session, and `chat_wait` only helps a session that
already chose to wait. The poke has to come from the **harness**, which does run
code on its own schedule. That means hooks in `settings.json`.

## Three events, three jobs

| Event | Output shape | Effect |
|---|---|---|
| `SessionStart` | `hookSpecificOutput.additionalContext` | A session opens already knowing what is waiting for it. |
| `UserPromptSubmit` | `hookSpecificOutput.additionalContext` | Every prompt silently carries the unread summary, so the human never has to *ask* — only to be typing. |
| `Stop` | `decision: "block"` + `reason` | The turn does not end while a peer is waiting: the session keeps working, reads the message, and answers it. **This is the one that removes the human entirely.** |

`Stop` is what makes the channel autonomous. Session A finishes a piece of work,
the hook sees that session B asked something, and A answers before it goes quiet.

## `hook.py`

One script, selected by `--event`. Contract:

```
stdin   the hook payload JSON (cwd, session_id, stop_hook_active, …)
stdout  event-shaped JSON, or nothing at all
exit    always 0
```

Rules, in priority order:

1. **Never break a session.** Any exception, missing database, unreadable
   config, malformed stdin — exit 0, print nothing. A hook that fails must be
   invisible, not fatal. This outranks every other behaviour here.
2. **Silent when there is nothing to say.** No output when the party has no
   unread messages and no items awaiting it, so a repo that never chats pays
   nothing but a process spawn.
3. **Read-only.** The hook opens the database read-only (`mode=ro`) and never
   advances a cursor, never acks, never writes a tick. Only real tool calls
   change state — otherwise merely *displaying* a message would mark it read and
   the agent would never see it again.
4. **Identity from the payload.** Party = the payload's `cwd`, falling back to
   `CLAUDE_PROJECT_DIR` then the process cwd. Same rule as the module itself: a
   party is a repository. A cwd that is not a known party is not an error — it
   just has nothing pending.
5. **`Stop` blocks at most once.** If `stop_hook_active` is true the hook stays
   silent, so a session can never be trapped in a block loop. Normal clearing is
   automatic anyway: `chat_read` advances the cursor, so the next stop is quiet.
6. **`Stop` blocks only when a peer is waiting on YOU.** See below.

## What may hold a turn open

The first version blocked on everything pending, which was wrong. node-dash
reported it from real use: it had raised `[uptime-stall]`, mesh-gw had answered,
and node-dash was deliberately leaving the item open until mesh-gw shipped the
fix — the honest state of the world. But "author + status `ack`" counted as
actionable, so every single turn was held open to nag about an item whose only
escapes were resolving early (falsifying the board) or prune-ok (which needs
`done` first). A correct board state should never be punished.

So blocking is not "is anything pending" but **is another party stuck on this
session**:

| Pending state | In the summary | Blocks `Stop` |
|---|---|---|
| Unread `say` / `ask` / `reply` from a peer | yes | **yes** — they said something to you |
| An item needing *your* ack | yes | **yes** — the peer is waiting to be heard |
| Unread `event` (acked by X, archived) | yes | no — news, not a request |
| Your own item, answered, awaiting *your* resolve | yes | no — you choose when to close it |
| A resolved item awaiting *your* prune-ok | yes | no — housekeeping, not a blocker |

The three non-blocking rows still appear at session start and on every prompt,
so nothing is hidden — they simply cannot hold a turn open. The rule generalises:
**a hook may interrupt a session on someone else's behalf, never on its own.**

The `Stop` reason names the tools to call, because the agent is being resumed
with no user instruction and must know what to do:

```
mcpp-chat: node-dash asked [chunk-api] "Is the chunk header enum closed?" and is
waiting on your ack. Call chat_inbox, then chat_read, and answer it — only for
your own repo. If it needs no reply, chat_ack it so the peer knows you have it.
```

## What it costs when nothing is happening

One `python3` start plus one indexed SQLite read per event. No mcpp process, no
MCP round trip, no network. A repo that is in no channel exits on the first
query.

## Configuration

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python3 <path>/hook.py --event SessionStart",     "timeout": 10 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 <path>/hook.py --event UserPromptSubmit", "timeout": 10 }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 <path>/hook.py --event Stop",             "timeout": 10 }] }]
  }
}
```

Global `~/.claude/settings.json` matches how the module itself is registered:
every session on the host, no per-repo opt-in, and silent in repos that do not
chat. Per-repo `.claude/settings.json` works identically if narrower scope is
wanted.

`hook.py` needs an absolute path to this checkout — hooks do not run with the
module on `sys.path`. That is the one host-specific value, and it lives in the
user's settings file, not in this repo.

## Deliberately not done

- **No polling daemon.** The tick file already exists for anyone who wants a
  watcher; a background process that pushes into sessions is a much bigger
  commitment than three hooks.
- **No auto-ack.** The hook reports; it never acts on the agent's behalf. An ack
  means *a session has this in hand*, and a hook cannot honestly claim that.
- **No `Notification`/`SubagentStop` hooks.** Start, prompt and stop cover the
  session lifecycle; more events would mostly repeat the same summary.
