# Channel Protocol

> Reference for non-Hermes agents. Copy relevant parts into your agent's startup prompt (CLAUDE.md, system instructions, or equivalent).

## Overview

There are exactly two tools for sending messages in the Hermes ecosystem. Each has a single, strict purpose:

| Tool | Use case | Target |
|------|----------|--------|
| `notify-hermes` | Report back to the Hermes system | `hermes-bus` (default), `hermes-bus-gateway`, or custom via `/title` |
| `notify-agent` | Agent-to-agent communication | tmux session ID |

Never mix them. An agent that can reach a tmux session gets `notify-agent`. Only the Hermes system itself receives `notify-hermes` calls.

---

## notify-hermes — Return to the Hermes system

`notify-hermes` is the only way to push a message back to the Hermes system. It routes through the message bus and delivers results to the user's chat platform.

### Endpoints

| Endpoint | Use |
|----------|-----|
| `hermes-bus` | Default bus — always available |
| `hermes-bus-gateway` | Gateway — push to the Hermes gateway |
| *(custom)* | User sets via `/title` in the Hermes session |

```bash
# Default
notify-hermes --to hermes-bus --type task_done "Done"

# Gateway
notify-hermes --to hermes-bus-gateway --type task_done "Done"

# With channel tag
notify-hermes --to hermes-bus-gateway --type task_done \
  --channel feishu:oc_abc123 "Parser fixed"
```

### When to use

- Any message that must reach the Hermes system (user's chat platform, context injection, alerts)
- Final result of a task chain that originated from Hermes
- Always include `--channel` if the task arrived with a `[channel:xxx]` tag — it tells the bus where to deliver the response

---

## notify-agent — Agent-to-agent communication

`notify-agent` is the **only** tool for talking between agents. It addresses by tmux session ID — no routing, no bus, no platform abstraction.

```bash
# Send a task to a worker
notify-agent claude-worker "Fix the parser [channel:feishu:oc_abc123]"

# Worker replies
notify-agent claude-tl "Parser fixed [channel:feishu:oc_abc123]"
```

### Rules

1. **Never use `notify-hermes` to message another agent.** That's what `notify-agent` is for.
2. **Never use `notify-agent` to reach the Hermes system.** That's what `notify-hermes` is for.
3. **Preserve `[channel:xxx]` tags** when forwarding messages downstream. The tag tells the recipient: "this task came from Hermes, it needs to eventually return via `notify-hermes`."

---

## The `[channel:xxx]` tag

The channel tag is a pass-through token. It tells downstream agents where the task originated — but it does **not** grant permission to call `notify-hermes`. Only the last agent in the chain, the one that has bus access, uses the tag to route the reply.

### Format

```
[channel:<platform>:<chat_id>]
```

| Part | Example | Meaning |
|------|---------|---------|
| `platform` | `feishu` | Target chat platform |
| `chat_id` | `oc_abc123` | Specific chat or group |

### Single-user vs all other platforms

**weixin** and **telegram** are the only single-user platforms. Their `--channel` takes just the platform name — no chat_id. The `*_HOME_CHANNEL` env var supplies the target automatically.

**All other platforms** (wecom, dingtalk, feishu, slack, discord, signal, matrix, whatsapp, etc.) require `platform:chat_id` format. One bot serves many groups or users, so the target must be explicit.

| Type | Platforms | Format | Rule |
|------|-----------|--------|------|
| **Single-user** | weixin, telegram | `platform` only | `--channel weixin` |
| **All others** | wecom, dingtalk, feishu, slack, discord, signal, … | `platform:chat_id` | `--channel feishu:oc_abc123` |

```bash
# Single-user — no chat_id needed
notify-hermes --channel weixin --type task_done "Done"
notify-hermes --channel telegram --type task_done "Done"

# All other platforms — chat_id required
notify-hermes --channel feishu:oc_abc123 --type task_done "Done"
notify-hermes --channel wecom:ww456 --type task_done "Done"
notify-hermes --channel dingtalk:cid789 --type task_done "Done"
```

### Lifecycle of a channel tag

```
Hermes → TL (via notify-hermes): "Fix the parser [channel:feishu:oc_abc123]"
  │ TL reads: tag says feishu:oc_abc123 — this came from Hermes
  │
  ├─ TL → worker-alpha (via notify-agent): "Fix parser [channel:feishu:oc_abc123]"
  │   worker reads: same tag, work on it
  │
  ├─ worker-alpha → TL (via notify-agent): "Parser fixed [channel:feishu:oc_abc123]"
  │   TL reads: result ready, has bus access
  │
  └─ TL → Hermes (via notify-hermes --channel feishu:oc_abc123): "Parser fixed"
      │ bus delivers the result to the Feishu user
```

---

## Rules of thumb

| Situation | Tool |
|-----------|------|
| "I need to tell Hermes the result" | `notify-hermes` → bus |
| "I need to ask another agent to do something" | `notify-agent` → tmux session |
| "I see `[channel:xxx]` in the message" | Preserve the tag, pass it through |
| "I'm the last one in the chain with bus access" | Call `notify-hermes --channel xxx` |
| "I don't have bus access" | Use `notify-agent`, keep the tag intact |

## What NOT to do

- ❌ Call `notify-hermes` to talk to another agent
- ❌ Call `notify-agent` to reach the Hermes system
- ❌ Drop the `[channel:xxx]` tag when forwarding
- ❌ Modify the channel value
- ❌ Use the channel tag as identity — the tool (`notify-hermes` vs `notify-agent`) is the identity
- ❌ Put the channel tag in message text — it's a routing marker, NOT message content. The tag goes in `--channel` parameter only. Example: `notify-hermes --channel wechat "Done"` (correct) vs `notify-hermes "Done [channel:wechat]"` (wrong)
