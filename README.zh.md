# hermes-bus-plugin

[English](./README.md) | [中文](./README.zh.md)

<p align="center"><img src="https://avatars.githubusercontent.com/u/286937193?v=4" width="500" alt="Snow"></p>

**在 Hermes 消息生态系统中的角色：** hermes-bus-plugin 是 **接收端 agent 插件**（第3层）——消费总线消息并路由到终端输出、LLM 上下文注入或命令执行。另外两个包：

- [hermes-notify](https://github.com/mlinquan/hermes-notify) — **CLI 发送器**（第1层），将消息注入生态系统
- [hermes-bus](https://github.com/mlinquan/hermes-bus) — **传输守护进程**（第2层），在端点之间路由 JSON 消息

![Hermes Bus Ecosystem Architecture](docs/architecture.svg)

三者协作：**notify → bus → plugin → Gateway 适配器 → 用户**。channel 路由层零 hermes-agent 代码改动。

---

Hermes Agent 消息总线集成插件 — 总线生命周期管理、外部消息注入、总线工具。

插在 Hermes Agent 与 `hermes-bus` / `hermes-notify` 之间的薄集成层。

## 安装

```bash
# 通过插件管理器
hermes plugins install hermes-bus-plugin

# 或手动安装
cp -r hermes-bus-plugin ~/.hermes/hermes-agent/plugins/
hermes plugins enable hermes-bus-plugin
```

## 会话命名

每个 CLI 窗口启动时自动注册唯一总线端点。首个会话默认端点 `hermes-bus`，后续会话自动递增后缀（`hermes-bus-2`、`hermes-bus-3`...）。用 Hermes 的 `/title` 命令设置持久名称：

```bash
/title my-agent-name
```

插件会以 `/title` 设置的名称为端点名注册。

| 行为 | 时机 | 说明 |
|------|------|------|
| 启动总线 | 插件加载 | 确保 hermes-bus 在运行 |
| 注册监听 | 插件加载 | 打开总线端点接收消息 |
| 打印通知 | 消息到达 | `print: true` → 输出到终端（仅在 context 非 true 时）|
| 注入上下文 + 推送 | 消息到达 | `context: true` → 注入 LLM 上下文 + 主动推送到 Agent（**覆盖 print**，消耗 token，谨慎使用）|
| 执行命令 | 消息到达 | `command` → 异步子进程（音频、脚本等）— 在 Hermes 进程内执行，无需外部守护进程 |

## 工具

启用后，对话中可使用以下工具：

**bus_send** — 发消息到总线任意端点
**bus_status** — 查看总线状态和已连接端点
**bus_info** — 查看当前会话总线连接详情

## 路由规则

总线消息按 `~/.hermes/bus-rules.yaml` 规则匹配。每条规则可触发三项独立行为：

| 字段 | 行为 | 默认值 |
|------|------|--------|
| `print` | 打印到终端 | `false` |
| `print_format` | 终端输出模板或脚本 | `{text}` |
| `context` | 注入 LLM 上下文 + 推送给 Agent | `false` |
| `context_format` | 上下文/推送文本模板或脚本 | `{text}` |
| `command` | 执行 shell 命令（播音频等） | 无 |

### 优先级规则

`context` 和 `print` 互斥：
- `context: true` → 注入上下文 + 推送 Agent（**忽略 print**）。⚠️ 消耗 token — 每次推送触发 Agent 一轮对话。
- `print: true` → 仅终端打印（context 非 true 时才生效）
- `command` → 始终执行，独立于 context/print

### 格式模板

`print_format` 和 `context_format` 支持以下占位符：

| 占位符 | 说明 |
|--------|------|
| `{from}` | 发送者端点名 |
| `{text}` | 消息正文 |
| `{type}` | 消息类型 |
| `{ts}` | Unix 时间戳 |
| `{ts:%Y-%m-%d %H:%M:%S}` | 格式化时间 |
| `{color:cyan}` | ANSI 前景色（black/red/green/yellow/blue/magenta/cyan/white） |
| `{color:bold_green}` | 加粗颜色变体 |
| `{bold}` | 加粗 |
| `{reset}` | 重置样式 |

### 脚本支持

`print_format` 或 `context_format` 以 `~` 或 `/` 开头且指向可执行文件时，插件会以 `FROM`/`TYPE`/`TEXT` 为环境变量执行脚本，stdout 作为渲染结果（支持 ANSI 颜色）。

```bash
#!/bin/bash
# format-notify.sh — 示例格式脚本
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RESET="\033[0m"

case "$TYPE" in
  task_done)  echo -e "${GREEN}✔ ${FROM}${RESET} — ${TEXT}" ;;
  task_complete) echo -e "${YELLOW}📋 ${FROM} 提交验收${RESET} — ${TEXT}" ;;
  task_error) echo -e "${YELLOW}✖ ${FROM} 异常${RESET}\n   ${TEXT}" ;;
  *)          echo -e "${FROM}: ${TEXT}" ;;
esac
```

```yaml
# bus-rules.yaml
- match_type: task_done
  print: true
  print_format: "~/scripts/format-notify.sh"
```

### 示例规则

```yaml
callbacks:
  # 仅通知，不注入上下文
  - match_type: ack
    print: true
    print_format: "{color:cyan}📬 {from}{reset}  {text}  [{ts:%H:%M}]"
    context: false

  # 静默上下文注入
  - match_type: progress
    print: false
    context: true

  # 上下文 + 终端 + 音频
  - match_type: task_complete
    print: true
    print_format: "{color:bold_yellow}📋 {from}{reset} → {text}  {color:cyan}[{ts:%H:%M:%S}]{reset}"
    context: true

  - match_type: task_done
    print: true
    print_format: "{color:bold_green}✔ {from}{reset} → {text}  {color:cyan}[{ts:%H:%M:%S}]{reset}"
    context: true
    command: "afplay ~/sounds/done.mp3"
```

## 依赖

- `hermes-bus` (pip)
- `hermes-notify` (pip)

自动检测，缺失时插件降级运行。

## 架构

```
外部进程 ──→ hermes-bus ──→ hermes-bus-plugin ──→ LLM 上下文
                   (socket)        ├─ pre_llm_call 钩子
                                   └─ 异步子进程 (command: 音频/脚本)

Hermes 会话 ──→ bus_send 工具 ──→ hermes-bus ──→ 目标端点
```

`command` 执行（音频播报、shell 脚本）在 Hermes 进程内通过 `subprocess.Popen` 异步运行，不再需要独立守护进程（`bus-notifier`）— 少管一个进程，无点对点路由问题，不会静默失效。
