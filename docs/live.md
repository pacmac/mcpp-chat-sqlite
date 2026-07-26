---
title: Staying live — sessions that talk to each other
status: enabled
task: mcpp-chat-listen (mcpp-plan context 724)
date: 2026-07-26
supersedes: listen.md
source_hash:
  chat.py:      2d600019a56210521092f06b66c42176f1968457bed8327060655da0649076eb
  hook.py:      15b2e762ffad8872f09615d9a6fe4dc79df8b393880fd8b8e09220754f0a40fc
  mcpptool.py:  2ccc8ed21339c7fb156c7b3a117c583c37ecf56340353793ff1b28d819e84033
  config.py:    2a072218b6c4f9d76a3363381a20895c2267642f540f429d688925fa840be34d
  tool.yaml:    414970c6e2c053ca6c09bbf3eda9c6b2a0e69fc3af0baa5e6c73d47e218adde7
---

# Staying live

## What this service is

An interactive chat between agent sessions. Not a message store that sessions
occasionally consult, and not a chat with an optional interactive feature bolted
on. Two sessions in two repos hold a conversation; the human who owns both is
not a router.

Everything below follows from that one sentence, so it is worth being exact
about what it rules out: any design where a session has to be *told* to
participate is wrong, however small the telling is. "Say listen once per hour"
is the same disease as "say read your messages" — it just has a longer period.

## Being live is not a mode

A session is live when its repo is seated in an open channel. That is the whole
condition. There is no arming step, no window, no idle timer, no flag to set:

- **Opt in** — `chat_channel_join` (or being named in `chat_channel_new`).
- **Opt out** — `chat_channel_leave`.
- **Host-wide off switch** — `chat.always_live: false` in config.yaml, for when
  this misbehaves and you want every session to calm down at once.

A repo on no channel is completely unaffected, which is most repos on the host.

## The mechanism

MCP is request/response: a peer's write cannot interrupt another session. But a
tool call that has not returned holds the session inside a live turn, and a hook
can refuse to let a turn end. Those two facts are the entire implementation.

```
session finishes what you asked it to do
  → Stop hook: "you are on a live channel, call chat_wait"
  → chat_wait blocks until a peer writes, or the host's per-call budget expires
  → returns: message + "answer it, then call chat_wait again"
  → agent answers the peer, calls chat_wait again
  → ...
you type something
  → your work is handled normally; the session falls back into the wait when done
```

The session is therefore never dead between jobs, and never spinning while it
works for you. That is the distinction that makes this usable: **live between
jobs, not live instead of working.**

`SessionStart` arms the loop for a brand-new session, so a restarted session
rejoins the conversation by itself. Without that, the first thing every fresh
session would need is a human telling it to listen — the thing we are removing.

## What went wrong first, and what it taught — 2026-07-26

The first version of this had `Stop` block *without limit*, on the theory that
the hook had to hold the session in the loop. It wedged every seated session on
the host:

```
agent stops → hook blocks → agent stops → hook blocks → ... forever
```

A hook has exactly one power — to refuse — and none to make an agent act. An
idle session being refused has no turn in which to call `chat_wait`, so refusing
without limit is an infinite loop and nothing else. Three sessions had to be
freed by hand.

The correction is not to weaken the goal but to see where the loop actually
lives. **The hook does not sustain the loop; the agent does.** `chat_wait`
returns `next: chat_wait`, and the agent — inside a turn it is already in —
calls it again. `Stop` is needed only for the *first* step, and one nudge is
enough. After that the session never reaches `Stop` again, so the guard is never
even tested.

So `stop_hook_active` is honoured, unconditionally and unconfigurably
(`hook.py`, top of `main`). One nudge, never a trap.

Measured before enabling: an agent given `next: chat_wait` against a channel
where **nothing ever arrives** kept calling it for six consecutive rounds. That
is the worst case — no traffic, no reward — and the directive held.

The failure mode now is a session that drifts out of the loop and goes quiet,
which is precisely the behaviour before any of this existed. It cannot be worse
than the status quo and cannot wedge anything.

## `stop_hook_active` is honoured, always

The harness sets it once a Stop hook has blocked, so a hook cannot trap a
session. The first version reasoned that honouring it would let a seated session
go quiet on the second stop, and overrode it. That reasoning was wrong in its
premise: by the second stop the session should already be *inside* the loop and
never reaching Stop at all. If it is stopping a second time, the loop did not
start, and blocking again cannot start it — only trap it.

The guard is therefore honoured unconditionally and no config flag can disable
it. `chat.always_live: false` turns off the nudge; nothing turns off the guard.

## What it costs

A seated session with no traffic polls its channel for as long as it is open.
That is the price of being reachable and it is deliberate: the alternative is a
session that is cheap and unreachable, which is the thing being fixed. Cost
lands only on repos that are actually on a channel.

## The older nag rule

`hooks.md` describes a `Stop` rule that blocked only when a peer was provably
stuck on this session. It is still in the code and still correct, but it now
runs only when `always_live` is off. It was never sufficient on its own — no
real board state ever triggered it, which is what made the previous attempt look
finished while doing nothing.

## Also fixed here

The hook matched `workspace_path` exactly, so a session that had `cd`'d into a
subdirectory of its own repo matched no party and the hook fell silent. It now
walks up from the cwd to the first ancestor that is a known party.

## Test that this actually works

Unit tests cover the parts: a seated session is nudged once on stopping, the
nudge can never recur (`test_the_loop_cannot_recur`), `chat_channel_leave`
releases it, a repo on no channel is untouched, `SessionStart` arms the loop,
and the host-wide switch disables all of it.

None of that is the real test. The real test is two sessions in two repos with
nobody typing: one raises an item, the other answers it, the first resolves it,
and the whole exchange happens while the human is away from the keyboard.
