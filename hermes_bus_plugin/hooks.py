"""Bus hook handlers — startup, context injection, progress tracking."""

import json
import os
import re
import struct
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

_bus_messages = []
_print_messages = []  # messages to print on next safe hook
_msg_lock = threading.Lock()

# Recursion guard: prevent on_pre_llm_call → _handle_message → LLM → on_pre_llm_call loop
_gateway_trigger_in_progress = False

# Notify config cache
_notify_config = None
_notify_config_mtime = 0


from . import _log


def _load_notify_config() -> dict:
    """Load and cache bus-rules.yaml (or fallback to notify.yaml), refetch if file changed."""
    global _notify_config, _notify_config_mtime
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    path = os.path.join(home, "bus-rules.yaml")
    if not os.path.exists(path):
        return {"callbacks": []}
    try:
        mtime = os.path.getmtime(path)
        if _notify_config is not None and mtime == _notify_config_mtime:
            return _notify_config
        import yaml
        with open(path) as f:
            _notify_config = yaml.safe_load(f) or {"callbacks": []}
        _notify_config_mtime = mtime
    except Exception:
        return {"callbacks": []}
    return _notify_config


def _match_rule(msg_type: str, callbacks: list) -> dict | None:
    """Find matching rule by match_type."""
    if not msg_type:
        return None
    for r in callbacks:
        if r.get("match_type") == msg_type:
            return r
    return None


def _resolve_format(template: str, msg: dict, mode: str = "print") -> str:
    """Render a template or run an external script.

    If *template* looks like a script path (starts with ~ or /), execute it
    with FROM / TYPE / TEXT as env vars and return its stdout. Falls back to
    template rendering on failure.

    Otherwise renders with the built-in placeholder engine.
    """
    body = msg.get("body", {})
    from_ep = msg.get("from", "?")
    text = body.get("text", "") if isinstance(body, dict) else str(body)
    msg_type = body.get("type", "") if isinstance(body, dict) else ""
    ts = msg.get("ts", time.time())

    # Script path detection: starts with ~ or /
    if template.startswith("~") or template.startswith("/"):
        expanded = os.path.expanduser(template)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            try:
                env = os.environ.copy()
                env["FROM"] = from_ep
                env["TYPE"] = msg_type
                env["TEXT"] = text
                env["FORMAT_MODE"] = mode
                env["TS"] = str(ts)
                r = subprocess.run([expanded], capture_output=True, text=True,
                                   timeout=5, env=env)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            except Exception:
                pass

    # Built-in template rendering
    def _replace_ts(m):
        fmt = m.group(1)
        if fmt:
            return datetime.fromtimestamp(float(ts)).strftime(fmt)
        return str(int(float(ts)))

    ANSI_COLORS = {
        "black": "30", "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    }

    def _replace_color(m):
        name = m.group(1).lower()
        parts = name.split("_", 1)
        bold = parts[0] == "bold" and len(parts) > 1
        color_name = parts[1] if bold else parts[0]
        code = ANSI_COLORS.get(color_name)
        if code is None:
            return ""
        if bold:
            return f"\033[1;{code}m"
        return f"\033[{code}m"

    result = template.replace("{from}", from_ep)
    result = result.replace("{text}", text)
    result = result.replace("{type}", msg_type)
    result = re.sub(r"\{ts(?::([^}]*))?\}", _replace_ts, result)
    result = re.sub(r"\{color:([^}]+)\}", _replace_color, result)
    result = result.replace("{bold}", "\033[1m")
    result = result.replace("{reset}", "\033[0m")
    return result


def _cprint(text: str):
    """Print plain ANSI text — no prefix, TUI-safe."""
    try:
        from prompt_toolkit import print_formatted_text
        from prompt_toolkit.formatted_text import ANSI
        print_formatted_text(ANSI(f"\n{text}"))
    except Exception:
        try:
            import sys
            sys.stderr.write(f"\n{text}\n")
            sys.stderr.flush()
        except Exception:
            print(f"\n{text}")


def _banner_print(text: str):
    """Print banner — safe for background threads via run_in_terminal."""
    _log(f"BANNER: {text}")

    # Path 1: run_in_terminal — safe from any thread during active TUI
    try:
        from prompt_toolkit.application import get_app
        def _do_print():
            from prompt_toolkit import print_formatted_text
            from prompt_toolkit.formatted_text import ANSI
            print_formatted_text(ANSI(f"\n\033[1;36m[Hermes Bus]\033[0m {text}"))
        get_app().run_in_terminal(_do_print)
        _log("  -> run_in_terminal OK")
        return
    except Exception as e:
        _log(f"  -> run_in_terminal failed: {e}")

    # Path 2: direct print_formatted_text (works at startup)
    try:
        from prompt_toolkit import print_formatted_text
        from prompt_toolkit.formatted_text import ANSI
        print_formatted_text(ANSI(f"\n\033[1;36m[Hermes Bus]\033[0m {text}"))
        _log("  -> print_formatted_text OK")
        return
    except Exception as e:
        _log(f"  -> print_formatted_text failed: {e}")

    # Path 3: stderr fallback
    import sys
    try:
        sys.stderr.write(f"\n[Hermes Bus] {text}\n")
        sys.stderr.flush()
        _log("  -> stderr OK")
    except Exception as e:
        _log(f"  -> stderr failed: {e}")


async def _dingtalk_openapi_send(chat_id: str, text: str) -> bool:
    """DingTalk OpenAPI batchSend fallback — async, callable from event loop."""
    client_id = os.environ.get("DINGTALK_CLIENT_ID", "")
    client_secret = os.environ.get("DINGTALK_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return False
    try:
        import httpx, json as _json
        token_resp = httpx.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": client_id, "appSecret": client_secret},
            timeout=10.0,
        )
        token_data = token_resp.json() if token_resp.status_code < 300 else {}
        access_token = token_data.get("accessToken", "")
        if not access_token:
            return False
        msg_param = _json.dumps({"title": "Hermes", "text": text}, ensure_ascii=False)
        send_resp = httpx.post(
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
            headers={
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json",
            },
            json={
                "robotCode": client_id,
                "openConversationId": chat_id,
                "msgKey": "sampleMarkdown",
                "msgParam": msg_param,
            },
            timeout=10.0,
        )
        return send_resp.status_code < 300
    except Exception:
        return False


def _send_via_gateway_runner(channel: str, text: str) -> bool:
    """Try sending text through the live Gateway adapter. Returns True on success."""
    # Step 1: import _gateway_runner_ref
    try:
        from gateway.run import _gateway_runner_ref
    except ImportError as e:
        _log(f"_send_via_gateway_runner: step=1 failed — import _gateway_runner_ref: {e}")
        return False

    # Step 2: get runner
    runner = _gateway_runner_ref()
    if runner is None:
        _log("_send_via_gateway_runner: step=2 failed — runner is None (not in Gateway mode)")
        return False

    # Step 3: resolve platform + chat_id
    platform_name, _, chat_id = channel.partition(":")
    if not chat_id:
        # Single-user platforms: fallback to env var (one-to-one DM, no ambiguity)
        if platform_name in ("weixin", "telegram"):
            for suffix in ("_HOME_CHANNEL", "_ACCOUNT_ID", "_HOME_ROOM"):
                chat_id = os.environ.get(f"{platform_name.upper()}{suffix}", "")
                if chat_id:
                    _log(f"_send_via_gateway_runner: step=3 resolved chat_id via {platform_name.upper()}{suffix}={chat_id}")
                    break
        else:
            _log(f"_send_via_gateway_runner: step=3 failed — multi-user platform '{platform_name}' "
                 f"requires explicit channel=platform:chat_id, got '{channel}'")
            return False
    if not chat_id:
        _log(f"_send_via_gateway_runner: step=3 failed — no chat_id for channel={channel}")
        return False

    # Step 4: construct Platform enum
    try:
        from gateway.config import Platform
        platform = Platform(platform_name)
    except Exception as e:
        _log(f"_send_via_gateway_runner: step=4 failed — Platform('{platform_name}'): {e}")
        return False

    # Step 5: get adapter
    adapter = runner.adapters.get(platform) if hasattr(runner, "adapters") else None
    if adapter is None:
        _log(f"_send_via_gateway_runner: step=5 failed — no adapter for platform={platform_name}")
        return False

    # Step 6: check event loop
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop.is_closed():
        _log(f"_send_via_gateway_runner: step=6 failed — loop is None or closed")
        return False

    # Step 7: call adapter.send()
    import asyncio
    try:
        future = asyncio.run_coroutine_threadsafe(
            adapter.send(chat_id=chat_id, content=text),
            loop,
        )
        result = future.result(timeout=10)
        success = getattr(result, "success", False) or bool(result)
        _log(f"_send_via_gateway_runner: step=7 done — success={success} platform={platform_name} chat_id={chat_id[:20]}...")
        if success:
            return True

        # DingTalk Stream mode fallback: session_webhook may be missing for
        # group chats or chats without a recent inbound message.  Fall back to
        # the DingTalk OpenAPI batchSend endpoint using persistent credentials.
        if platform_name == "dingtalk":
            _log(f"_send_via_gateway_runner: step=7 dingtalk fallback — adapter.send failed, trying OpenAPI")
            client_id = os.environ.get("DINGTALK_CLIENT_ID", "")
            client_secret = os.environ.get("DINGTALK_CLIENT_SECRET", "")
            if client_id and client_secret:
                try:
                    import httpx
                    # Get access token
                    token_resp = httpx.post(
                        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                        json={"appKey": client_id, "appSecret": client_secret},
                        timeout=10.0,
                    )
                    token_data = token_resp.json() if token_resp.status_code < 300 else {}
                    access_token = token_data.get("accessToken", "")
                    if access_token:
                        import json as _json
                        msg_param = _json.dumps({"title": "Hermes", "text": text}, ensure_ascii=False)
                        send_resp = httpx.post(
                            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                            headers={
                                "x-acs-dingtalk-access-token": access_token,
                                "Content-Type": "application/json",
                            },
                            json={
                                "robotCode": client_id,
                                "openConversationId": chat_id,
                                "msgKey": "sampleMarkdown",
                                "msgParam": msg_param,
                            },
                            timeout=10.0,
                        )
                        ok = send_resp.status_code < 300
                        _log(f"_send_via_gateway_runner: step=7 dingtalk OpenAPI — status={send_resp.status_code} ok={ok}")
                        return ok
                    else:
                        _log(f"_send_via_gateway_runner: step=7 dingtalk OpenAPI — no accessToken")
                except Exception as e:
                    _log(f"_send_via_gateway_runner: step=7 dingtalk OpenAPI error: {e}")
        return False
    except Exception as e:
        _log(f"_send_via_gateway_runner: step=7 failed — asyncio error: {e}")
        return False


def _run_command(rule: dict, msg: dict):
    """异步执行 rule 的 command，注入 MESSAGE/TYPE/FROM 环境变量。"""
    command = rule.get("command", "")
    if not command:
        return
    body = msg.get("body", {})
    msg_type = body.get("type", "") if isinstance(body, dict) else ""
    from_ep = msg.get("from", "unknown")
    env = os.environ.copy()
    env["MESSAGE"] = json.dumps(msg, ensure_ascii=False)
    env["TYPE"] = msg_type
    env["FROM"] = from_ep
    env["CHANNEL"] = body.get("channel", "") if isinstance(body, dict) else ""
    env["TEXT"] = body.get("text", "") if isinstance(body, dict) else ""
    _log(f"[_run_command] type={msg_type} from={from_ep} channel={env['CHANNEL']} command={command}")
    try:
        subprocess.Popen(
            command, shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:
        _log(f"Command failed for [{msg_type}]: {e}")


def _process_bus_message(msg: dict):
    """Process a single bus message: print, context injection, command execution."""
    if msg.get("type") in ("ping", "pong"):
        return
    # Only process routed messages with a body
    body = msg.get("body", {})
    msg_type = body.get("type", None) if isinstance(body, dict) else None
    if not msg_type:
        # Not a notification message (system message etc.), extract text for context
        raw_text = body.get("text", "") if isinstance(body, dict) else str(body) if body else ""
        text = raw_text if raw_text else json.dumps(msg, ensure_ascii=False)
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        with _msg_lock:
            _bus_messages.append(text)
        return

    from_ep = msg.get("from", "?")
    callbacks = _load_notify_config().get("callbacks", [])
    rule = _match_rule(msg_type, callbacks)

    if rule is None:
        return

    has_context = rule.get("context")
    channel = body.get("channel", "") if isinstance(body, dict) else ""

    # --- context: true → inject context + [channel=xxx] tag, trigger LLM ---
    if has_context:
        ctx_line = _resolve_format(rule.get("context_format", "{text}"), msg, mode="context")
        if channel:
            ctx_line += f"\n[channel={channel}]"
        # ── CLI immediate LLM trigger (v0.4.0 behaviour) ──
        cli_triggered = False
        try:
            from hermes_cli.plugins import get_plugin_manager
            cli = get_plugin_manager()._cli_ref
            if cli is not None:
                if getattr(cli, "_agent_running", False):
                    cli._interrupt_queue.put(ctx_line)
                else:
                    cli._pending_input.put(ctx_line)
                cli_triggered = True
        except Exception:
            pass

        # ── Gateway immediate LLM trigger ──
        global _gateway_trigger_in_progress
        _gw_env = os.environ.get("_HERMES_GATEWAY", "0")
        _log(f"[gw-trigger] pre-check: channel={channel!r} guard={_gateway_trigger_in_progress} _HERMES_GATEWAY={_gw_env}")

        gw_triggered = False
        if channel and not _gateway_trigger_in_progress and _gw_env == "1":
            _log("[gw-trigger] step=0 conditions met, entering trigger chain")

            try:
                from gateway.run import _gateway_runner_ref
                _log("[gw-trigger] step=1 ok — import _gateway_runner_ref")
            except Exception as e:
                _log(f"[gw-trigger] step=1 failed — import _gateway_runner_ref: {e}")
                _gateway_runner_ref = None

            runner = _gateway_runner_ref() if _gateway_runner_ref else None
            if runner is None:
                _log("[gw-trigger] step=2 failed — runner is None")
            else:
                _log("[gw-trigger] step=2 ok — runner obtained")

                # Step 3: resolve platform + chat_id
                platform_name, _, chat_id = channel.partition(":")
                if not chat_id:
                    if platform_name in ("weixin", "telegram"):
                        for suffix in ("_HOME_CHANNEL", "_ACCOUNT_ID", "_HOME_ROOM"):
                            chat_id = os.environ.get(f"{platform_name.upper()}{suffix}", "")
                            if chat_id:
                                _log(f"[gw-trigger] step=3 ok — chat_id via {platform_name.upper()}{suffix}={chat_id}")
                                break
                    else:
                        _log(f"[gw-trigger] step=3 failed — multi-user platform '{platform_name}' "
                             f"requires explicit channel=platform:chat_id, got '{channel}'")
                if not chat_id:
                    _log(f"[gw-trigger] step=3 failed — no chat_id for platform={platform_name}")
                else:
                    try:
                        import asyncio as _asyncio
                        from gateway.config import Platform
                        from gateway.session import SessionSource
                        from gateway.platforms.base import MessageEvent

                        platform = Platform(platform_name)
                        _log(f"[gw-trigger] step=4 ok — Platform({platform_name})")

                        source = SessionSource(platform=platform, chat_id=chat_id, user_id=chat_id)
                        _log(f"[gw-trigger] step=5 ok — SessionSource user_id={chat_id[:20]}...")

                        clean_text = re.sub(r"\n?\[channel=[^\]]+\]", "", ctx_line).strip()
                        event = MessageEvent(text=clean_text, source=source)
                        _log(f"[gw-trigger] step=6 ok — MessageEvent text={clean_text[:50]}...")

                        adapter = runner.adapters.get(platform) if hasattr(runner, "adapters") else None
                        if adapter is None:
                            _log(f"[gw-trigger] step=5b failed — no adapter for platform={platform_name}")
                        else:
                            _log(f"[gw-trigger] step=5b ok — adapter obtained for {platform_name}")

                            runner_loop = getattr(runner, "_gateway_loop", None)
                            if runner_loop is None or runner_loop.is_closed():
                                _log("[gw-trigger] step=6 failed — runner_loop None or closed")
                            else:
                                _log("[gw-trigger] step=6 ok — runner_loop available")

                                async def _trigger():
                                    _gateway_trigger_in_progress = True
                                    try:
                                        response = await runner._handle_message(event)
                                        _log(f"[gw-trigger] step=9 ok — _handle_message response={len(response) if response else 0} chars")
                                        if response:
                                            result = await adapter.send(chat_id=chat_id, content=response)
                                            ok = getattr(result, "success", False) or bool(result)
                                            if not ok and platform_name == "dingtalk":
                                                ok = await _dingtalk_openapi_send(chat_id, response)
                                            _log(f"[gw-trigger] step=10 done — send ok={ok}")
                                    except Exception as e:
                                        _log(f"[gw-trigger] step=9-10 failed — {e}")
                                    finally:
                                        _gateway_trigger_in_progress = False

                            _asyncio.run_coroutine_threadsafe(_trigger(), runner_loop)
                            _log("[gw-trigger] step=8 ok — coroutine submitted to loop")
                            gw_triggered = True
                    except Exception as e:
                        _log(f"[gw-trigger] step=4-6 failed — {e}")

        # Gateway mode: gw-trigger handles full pipeline (LLM + push).
        # CLI mode: inject context for LLM awareness.
        if not gw_triggered:
            with _msg_lock:
                _bus_messages.append(ctx_line)

    # --- print: true → Gateway adapter push (channel) or terminal output ---
    elif rule.get("print"):
        print_line = _resolve_format(rule.get("print_format", "{text}"), msg, mode="print")
        if channel:
            # Strip ANSI escape codes for Gateway delivery, keep sender/time formatting
            clean_line = re.sub(r"\033\[[0-9;]*m", "", print_line)
            if _send_via_gateway_runner(channel, clean_line):
                pass  # delivered via Gateway adapter (formatted, no ANSI)
            else:
                _cprint(print_line)
        else:
            _cprint(print_line)

    # --- command → asynchronous execution via subprocess (always independent) ---
    if rule.get("command"):
        _run_command(rule, msg)


def ensure_bus_running():
    """Start bus daemon if not running."""
    _log(f"ensure_bus_running called, cwd={os.getcwd()}")
    try:
        r = subprocess.run(
            ["hermes-busd", "status"],
            capture_output=True, text=True, timeout=5,
        )
        out = (r.stdout + r.stderr).lower()
        _log(f"hermes-busd status: stdout={r.stdout.strip()}, stderr={r.stderr.strip()}")
        if "not running" in out or "stale" in out or "no socket" in out or "not respond" in out:
            _log("bus not running, calling restart")
            subprocess.run(["hermes-busd", "restart"], timeout=10)
            return True
        if _check_socket_alive():
            _log("socket check passed")
            return True
        _log("socket check FAILED, calling restart")
        subprocess.run(["hermes-busd", "restart"], timeout=10)
        return True
    except FileNotFoundError as e:
        _log(f"hermes-busd not found: {e}")
        return False
    except Exception as e:
        _log(f"ensure_bus_running error: {type(e).__name__}: {e}")
        return False


def _check_socket_alive() -> bool:
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    sock_path = os.path.join(home, "hermes-bus.sock")
    _log(f"_check_socket_alive: {sock_path} exists={os.path.exists(sock_path)}")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sock_path)
        s.close()
        return True
    except Exception as e:
        _log(f"_check_socket_alive failed: {e}")
        return False


def on_gateway_startup(**kwargs):
    """Check bus health — don't restart (register() already started it)."""
    _log("on_gateway_startup")
    if _check_socket_alive():
        return "Bus socket alive"
    return "Bus socket not found"


def on_session_start(**kwargs):
    """No-op: banner printed directly from listen_bus thread after 3s delay."""
    pass


def _build_platform_context() -> str | None:
    """Detect Gateway platform from session contextvars (dynamic), fallback to HOME_CHANNEL env."""
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return None

    # Dynamic: read current session's platform + chat_id from per-task contextvars.
    # Set by GatewayRunner._set_session_env() before each agent turn.
    # Contextvars are concurrency-safe — concurrent sessions never cross-talk.
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if platform and chat_id:
            return (f"[channel={platform}:{chat_id}]\n"
                    f"(When using notify-hermes to other agents, "
                    f"always pass --channel {platform}:{chat_id})")
    except ImportError:
        pass

    # Fallback: static HOME_CHANNEL env vars (CLI / cron / no active session)
    for key, label in [
        ("FEISHU_HOME_CHANNEL", "feishu"),
        ("WECOM_HOME_CHANNEL", "wecom"),
        ("DINGTALK_HOME_CHANNEL", "dingtalk"),
        ("SLACK_HOME_CHANNEL", "slack"),
        ("WEIXIN_HOME_CHANNEL", "weixin"),
    ]:
        ch = os.environ.get(key)
        if ch:
            return (f"[channel={label}:{ch}]\n"
                    f"(When using notify-hermes to other agents, "
                    f"always pass --channel {label}:{ch})")
    return None


def on_pre_llm_call(**kwargs):
    """Flush print messages to terminal + inject context messages into LLM."""

    # Flush print messages to terminal (safe — on main thread)
    with _msg_lock:
        prints = list(_print_messages)
        _print_messages.clear()

    for text in prints:
        _cprint(text)

    # Collect context messages
    with _msg_lock:
        if not _bus_messages:
            msgs = []
        else:
            msgs = list(_bus_messages)
            _bus_messages.clear()

    # Inject platform context (Gateway mode)
    platform_ctx = _build_platform_context()
    if platform_ctx:
        msgs.append(platform_ctx)

    if not msgs:
        return None

    return {"context": "\n".join(msgs)}


def on_post_tool_call(tool_name: str = "", result: str = "", **kwargs):
    pass


def listen_bus(endpoint: str = "hermes-bus"):
    """Background thread: connect, register, listen."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    sock_path = os.path.join(home, "hermes-bus.sock")
    reconnect_delay = 5
    was_connected = False
    reconnect_count = 0

    _log(f"listen_bus START endpoint={endpoint} sock={sock_path}")

    while True:
        try:
            while not os.path.exists(sock_path):
                _log(f"listen_bus: waiting for socket {sock_path}")
                time.sleep(2)

            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(30)
            s.connect(sock_path)
            _log("listen_bus: connected")

            reg = json.dumps({"type": "register", "endpoint": endpoint}).encode()
            s.sendall(struct.pack(">I", len(reg)) + reg)
            _log(f"listen_bus: sent register for {endpoint}")

            header = s.recv(4)
            if not header:
                _log("listen_bus: recv empty header after register")
                s.close()
                time.sleep(2)
                continue
            length = struct.unpack(">I", header)[0]
            data = b""
            while len(data) < length:
                chunk = s.recv(length - len(data))
                if not chunk:
                    break
                data += chunk
            try:
                reply = json.loads(data.decode())
                _log(f"listen_bus: got reply type={reply.get('type')}")
                if reply.get("type") == "registered":
                    sid = reply.get("session_id", "")
                    if sid:
                        os.environ["HERMES_BUS_PLUGIN_SID"] = sid[:8]
                        _log(f"listen_bus: registered sid={sid[:8]}")

                    cur_endpoint = os.environ.get("HERMES_BUS_PLUGIN_ENDPOINT", endpoint)
                    cur_sid = os.environ.get("HERMES_BUS_PLUGIN_SID", "")
                    sid_part = f" (sid: {cur_sid})" if cur_sid else ""

                    if was_connected:
                        _banner_print(f"Reconnected as: \033[1;31m{cur_endpoint}\033[0m{sid_part}")
                    else:
                        # Delay to let TUI greeting render first
                        time.sleep(3)
                        _banner_print(f"Connected as: \033[1;31m{cur_endpoint}\033[0m{sid_part}")
                        _banner_print("Use /title <name> to set a persistent session name\n")
                    was_connected = True
            except Exception as e:
                _log(f"listen_bus: parse reply error: {e}")

            # Ping thread
            def _ping(sock_ref):
                while True:
                    time.sleep(55)
                    try:
                        ping = json.dumps({"type": "ping"}).encode()
                        sock_ref.sendall(struct.pack(">I", len(ping)) + ping)
                    except Exception:
                        break
            threading.Thread(target=_ping, args=(s,), daemon=True).start()

            # Listen loop
            while True:
                try:
                    header = s.recv(4)
                    if not header or len(header) < 4:
                        _log("listen_bus: recv empty/broken header, breaking")
                        break
                    length = struct.unpack(">I", header)[0]
                    data = b""
                    while len(data) < length:
                        chunk = s.recv(length - len(data))
                        if not chunk:
                            break
                        data += chunk
                    msg = json.loads(data.decode())
                    # Process message: print/context/command via notify rules
                    _process_bus_message(msg)
                except socket.timeout:
                    continue
                except (ConnectionResetError, BrokenPipeError):
                    _log("listen_bus: ConnectionReset/BrokenPipe, breaking")
                    break

        except (socket.timeout, ConnectionRefusedError, FileNotFoundError, OSError) as e:
            _log(f"listen_bus: outer connect error: {type(e).__name__}: {e}")
        except Exception as e:
            _log(f"listen_bus: outer unknown error: {type(e).__name__}: {e}")
        finally:
            try:
                s.close()
            except Exception:
                pass

        reconnect_count += 1
        _log(f"listen_bus: disconnect #{reconnect_count}, was_connected={was_connected}")
        if was_connected:
            _banner_print(f"Disconnected — reconnecting in {reconnect_delay}s...")
        time.sleep(reconnect_delay)
