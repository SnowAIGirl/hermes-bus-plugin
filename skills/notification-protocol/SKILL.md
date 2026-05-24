---
name: notification-protocol
description: Unified cross-agent notification protocol via hermes-bus ecosystem. Covers notify-hermes, notify-agent, bus message format, --channel reply routing, and bus-rules.yaml callback configuration.
version: 1.0.0
metadata:
  hermes:
    tags: [bus, notification, notify-hermes, notify-agent, channel, routing, protocol]
---

# Notification Protocol

Unified notification protocol for the Hermes bus ecosystem. Three packages work together: notify (send) → bus (transport) → plugin (receive).

## Sending

### notify-hermes — through the bus

```bash
notify-hermes --to <endpoint> --type <type> [--channel <platform:chat_id>] "message"
```

Message types: `directive`, `ack`, `task_start`, `progress`, `task_complete`, `task_done`, `plan_ready`, `task_error`, `need_decision`.

Channel format:
- **weixin, telegram** (single-user): `platform` only — `*_HOME_CHANNEL` env var auto-fills chat_id
- **All other platforms**: `platform:chat_id` — must specify target group/user

### notify-agent — to a tmux session

```bash
notify-agent [--from SENDER] <tmux-session-name> "message"
```

Target is a tmux session name, not a bus endpoint. Create sessions with `tmux new-session -s <name> 'claude'`.

## Receiving

Route rules in `~/.hermes/bus-rules.yaml` control how messages are processed:

- `print: true` — terminal output
- `context: true` — inject into LLM context + push to Agent
- `command` — shell command (env: MESSAGE/TYPE/FROM/CHANNEL/TEXT/TS)

## Channel Routing

When `--channel` is set, the bus-plugin routes replies back to the originating chat platform:

```
gateway sets channel → agent echoes channel → bus carries channel → plugin delivers via adapter
```

Channel is an opaque token. Agents pass it through unmodified. Only the bus-plugin acts on it at final delivery.

**[channel:xxx] is a routing marker, NOT message content.** When calling `notify-hermes`, put the tag in `--channel` only — never include it in the message text. Wrong: `notify-hermes "Done [channel:wechat]"`. Correct: `notify-hermes --channel wechat "Done"`.

## Bus Message Format

4-byte BE length prefix + JSON body:

```json
{"type":"message","to":"lead-agent","from":"worker-alpha","ts":1716307200.123,"body":{"text":"hello","type":"ack","channel":"feishu:oc_abc123"}}
```

## bus-rules.yaml

```yaml
callbacks:
  - match_type: directive
    context: true
    context_format: "📬 [directive] {from}: {text}"

  - match_type: task_complete
    print: true
    print_format: "~/.hermes/scripts/format-bus-message"
    context: true
    context_format: "📬 {from} submitted for review: {text}"

  - match_type: task_done
    print: true
    print_format: "~/.hermes/scripts/format-bus-message"
    context: true
    context_format: "📬 {from} approved: {text}"
    command: "~/.hermes/scripts/play-notify-sound; ~/.hermes/scripts/gateway-forward"
```
