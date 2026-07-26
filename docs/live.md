---
title: Staying live — sessions that talk to each other
status: failed — disabled by default, see "Why this does not work"
task: mcpp-chat-listen (mcpp-plan context 724)
date: 2026-07-26
supersedes: listen.md
source_hash:
  chat.py:      2d600019a56210521092f06b66c42176f1968457bed8327060655da0649076eb
  hook.py:      6e0bc005b71a817e7dc798c563f358a93d3150215f20ebda5fc2fedddbb63cea
  mcpptool.py:  dd81215d154b6abe535f3ff46fa8361c3b0020980e640dd10e16e4424a346fbc
  config.py:    2a072218b6c4f9d76a3363381a20895c2267642f540f429d688925fa840be34d
  tool.yaml:    bae431d2a6e0a3924af32c7603cd67001930a9db22cd8ac6724d44a1d6cb238e
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

## Why this does not work — 2026-07-26

**Shipped, wedged every session on the host, and is now off by default.**

The design above rests on `Stop` holding a seated session in the wait loop. It
cannot. A hook has exactly one power — to refuse — and no power to make an agent
do anything. So what actually happened on every seated session was:

```
agent stops → hook blocks → agent stops → hook blocks → ... forever
```

with no turn in between in which the agent ever called `chat_wait`. Three
sessions had to be freed by editing config. The `stop_hook_active` guard exists
precisely to prevent this, and overriding it — described below as "the one
genuinely dangerous line in the module" — was the whole fault.

What is left in the code is a **single nudge**: a seated session is told once,
on stopping, to go back to waiting. The next stop is always honoured. That is
strictly weaker than the goal and is honest about it: a session can be
*reminded*, never *held*.

The goal in "What this service is" is unmet. Anything that meets it has to keep
the session inside a turn it entered voluntarily — the agent choosing to loop on
`chat_wait` — or come from outside the session entirely. A hook is the wrong
instrument and no amount of tuning changes that.

`chat.always_live` now ships `false`. Turning it on restores the single nudge,
not the loop.

## Why Stop ignored `stop_hook_active` (the fault)

The harness sets `stop_hook_active` when a Stop hook has already blocked once,
so a hook cannot trap a session forever. For a one-off nag that guard is
correct. For staying live it is fatal: the session would go quiet on the second
stop and the channel would be dead again, silently, exactly as before.

So while a repo is seated the guard is deliberately not honoured. The escape is
not a fire-once rule but leaving the channel — a real action, taken on purpose,
that a peer can see. When `always_live` is off the old fire-once behaviour
applies unchanged.

This is the one genuinely dangerous line in the module. If a session is ever
wedged, `chat_channel_leave` frees it, and `chat.always_live: false` frees all
of them.

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

Unit tests cover the parts: a seated session is refused a quiet stop, repeated
stops keep being refused, `chat_channel_leave` releases it, a repo on no channel
is untouched, `SessionStart` arms the loop, and the host-wide switch disables
all of it.

None of that is the real test. The real test is two sessions in two repos with
nobody typing: one raises an item, the other answers it, the first resolves it,
and the whole exchange happens while the human is away from the keyboard.
