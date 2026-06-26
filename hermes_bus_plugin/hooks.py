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


def _real_home() -> str:
    """Get real user home directory, immune to sandbox $HOME overrides."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


_bus_messages = []
_print_messages = []  # messages to print on next safe hook
_msg_lock = threading.Lock()

# Recursion guard: prevent on_pre_llm_call → _handle_message → LLM → on_pre_llm_call loop
_gateway_trigger_in_progress = False

# Source line inject-once guard: CLI agents need to know their endpoint,
# but injecting every turn wastes tokens. Inject once, first LLM call.
_source_line_injected = False

# ── Rate-limited trigger queue ──
# GW-trigger pushes to WeChat via iLink which rate-limits aggressively.
# Instead of firing immediately (or debouncing per-sender), use a single
# global queue with a minimum interval between triggers.  Burst messages
# arriving inside the window are merged; if messages arrive after the
# window closes they wait in the queue until the interval expires.
import collections
import queue as _stdlib_queue

_RATE_LIMIT_INTERVAL = 30.0  # minimum seconds between WeChat pushes

# (endpoint, channel, ctx_line) tuples
_trigger_queue: _stdlib_queue.Queue = _stdlib_queue.Queue()
_trigger_last_fire: float = 0.0
_trigger_timer: threading.Timer | None = None
_trigger_lock = threading.Lock()
_trigger_loop_started = False

# Notify config cache
_notify_config = None
_notify_config_mtime = 0


from . import _log


def _load_notify_config() -> dict:
    """Load and cache bus-rules.yaml (or fallback to notify.yaml), refetch if file changed."""
    global _notify_config, _notify_config_mtime
    home = os.environ.get("HERMES_HOME", os.path.join(_real_home(), ".hermes"))
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


def _start_trigger_consumer():
    """Background thread: drain the trigger queue at a fixed rate.

    Merges all pending items into one LLM trigger, then waits for the
    rate-limit interval before firing the next batch.  This caps WeChat
    pushes at ~1 per _RATE_LIMIT_INTERVAL seconds regardless of inbound
    message rate.
    """
    global _trigger_loop_started, _trigger_last_fire
    if _trigger_loop_started:
        return
    _trigger_loop_started = True

    def _consumer():
        global _trigger_last_fire
        while True:
            # Block until at least one item arrives
            first = _trigger_queue.get()

            # Drain all currently queued items
            items = [first]
            while True:
                try:
                    items.append(_trigger_queue.get_nowait())
                except _stdlib_queue.Empty:
                    break

            # Merge all context lines, prefixing each with its arrival timestamp
            merged_lines: list[str] = []
            channels: set[str] = set()
            for _ep, _ch, _line, _ts in items:
                channels.add(_ch)
                if _line and _line.strip():
                    from datetime import datetime
                    _ts_str = datetime.fromtimestamp(_ts).strftime("[%Y-%m-%d %H:%M:%S]")
                    merged_lines.append(f"{_ts_str} {_line.strip()}")

            if not merged_lines:
                continue

            # Use the first non-empty channel
            channel = next(iter(channels), "")
            if not channel:
                # Try auto-resolve
                try:
                    from gateway.run import _gateway_runner_ref as _gwref
                    _r = _gwref() if _gwref else None
                    if _r is not None and hasattr(_r, "adapters"):
                        for _plat, _adp in _r.adapters.items():
                            _acct = getattr(_adp, "_account_id", None) or ""
                            if _acct:
                                channel = _plat.value
                                break
                except Exception:
                    pass

            if not channel:
                _log("[gw-trigger] consumer: no channel, dropping batch")
                continue

            # ── Wait for rate-limit interval ──
            with _trigger_lock:
                elapsed = time.time() - _trigger_last_fire
                wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                _log(f"[gw-trigger] consumer: rate-limit wait {wait:.1f}s")
                time.sleep(wait)

            # ── Resolve platform + chat_id ──
            platform_name, _, chat_id = channel.partition(":")
            if not chat_id:
                if platform_name in ("weixin", "telegram"):
                    for suffix in ("_HOME_CHANNEL", "_ACCOUNT_ID", "_HOME_ROOM"):
                        chat_id = os.environ.get(f"{platform_name.upper()}{suffix}", "")
                        if chat_id:
                            break
            if not chat_id:
                _log(f"[gw-trigger] consumer: no chat_id for {platform_name}")
                continue

            # ── Build event ──
            try:
                import asyncio as _asyncio
                from gateway.run import _gateway_runner_ref as _gwref2
                from gateway.config import Platform
                from gateway.session import SessionSource
                from gateway.platforms.base import MessageEvent

                runner = _gwref2() if _gwref2 else None
                if runner is None:
                    _log("[gw-trigger] consumer: runner is None")
                    continue

                platform = Platform(platform_name)
                source = SessionSource(platform=platform, chat_id=chat_id, user_id=chat_id)

                merged_text = "\n\n".join(merged_lines)
                event = MessageEvent(text=merged_text, source=source)
                _log(f"[gw-trigger] consumer: firing {len(merged_lines)} merged lines → {merged_text[:80]}...")

                adapter = runner.adapters.get(platform) if hasattr(runner, "adapters") else None
                if adapter is None:
                    _log(f"[gw-trigger] consumer: no adapter for {platform_name}")
                    continue

                runner_loop = getattr(runner, "_gateway_loop", None)
                if runner_loop is None or runner_loop.is_closed():
                    _log("[gw-trigger] consumer: runner_loop None or closed")
                    continue
            except Exception as e:
                _log(f"[gw-trigger] consumer: setup failed — {e}")
                continue

            # ── Fire ──
            global _gateway_trigger_in_progress
            async def _trigger():
                nonlocal merged_lines, channel
                _gateway_trigger_in_progress = True
                ok = False
                try:
                    response = await runner._handle_message(event)
                    _log(f"[gw-trigger] consumer: response={len(response) if response else 0} chars")
                    if response:
                        result = await adapter.send(chat_id=chat_id, content=response)
                        ok = getattr(result, "success", False) or bool(result)
                        if not ok and platform_name == "dingtalk":
                            ok = await _dingtalk_openapi_send(chat_id, response)
                        _log(f"[gw-trigger] consumer: send ok={ok}")
                except Exception as e:
                    _log(f"[gw-trigger] consumer: trigger failed — {e}")
                finally:
                    _gateway_trigger_in_progress = False

                # ── Re-queue on failure: don't lose messages ──
                if not ok and response and merged_lines:
                    _log(f"[gw-trigger] consumer: send failed, re-queuing {len(merged_lines)} items")
                    for _line in merged_lines:
                        _trigger_queue.put(("retry", channel, _line, time.time()))

            with _trigger_lock:
                _trigger_last_fire = time.time()
            _asyncio.run_coroutine_threadsafe(_trigger(), runner_loop)
            _log(f"[gw-trigger] consumer: submitted, merged={len(merged_lines)} items")

    threading.Thread(target=_consumer, daemon=True, name="gw-trigger-consumer").start()
    _log("[gw-trigger] consumer thread started")


def _process_bus_message(msg: dict):
    """Process a single bus message: print, context injection, command execution."""
    if msg.get("type") in ("ping", "pong"):
        return
    # Only process routed messages with a body
    body = msg.get("body", {})
    from_ep = msg.get("from", "?")
    msg_type = body.get("type", None) if isinstance(body, dict) else None

    callbacks = _load_notify_config().get("callbacks", [])
    rule = _match_rule(msg_type, callbacks)

    has_context = rule.get("context") if rule else False
    should_print = rule.get("print", True) if rule else True
    channel = body.get("channel", "") if isinstance(body, dict) else ""

    # --- context: true → inject context, trigger LLM ---
    if has_context:
        ctx_line = _resolve_format(rule.get("context_format", "{text}"), msg, mode="context")
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
        if not _gateway_trigger_in_progress and _gw_env == "1":
            # ── Resolve channel when sender omitted --channel ──
            if not channel:
                try:
                    from gateway.run import _gateway_runner_ref as _gwref
                    _r = _gwref() if _gwref else None
                    if _r is not None and hasattr(_r, "adapters"):
                        for _plat, _adp in _r.adapters.items():
                            _acct = getattr(_adp, "_account_id", None) or ""
                            if _acct:
                                channel = _plat.value
                                _log(f"[gw-trigger] auto-resolved channel={channel} from adapter {_plat.value}")
                                break
                except Exception as _e:
                    _log(f"[gw-trigger] auto-resolve channel failed: {_e}")

            if channel and ctx_line:
                # ── Rate-limited trigger: push to global queue ──
                # Consumer thread drains at fixed interval, merging pending
                # items into one LLM trigger → one WeChat push per interval.
                _start_trigger_consumer()
                _trigger_queue.put((from_ep, channel, ctx_line, time.time()))
                _log(f"[gw-trigger] queued: from={from_ep} channel={channel} qsize={_trigger_queue.qsize()}")
                gw_triggered = True  # mark as handled (queued)

        # Gateway mode: gw-trigger handles full pipeline (LLM + push).
        # CLI mode: inject context for LLM awareness.
        # Guard: don't double-inject when CLI already handled it via interrupt/pending.
        if not gw_triggered and not cli_triggered:
            with _msg_lock:
                _bus_messages.append(ctx_line)

    # --- print handling (default true) ---
    elif should_print:
        print_line = _resolve_format(rule.get("print_format", "{text}") if rule else "{text}", msg, mode="print")
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
    if rule and rule.get("command"):
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
    root = os.environ.get("HERMES_BUS_ROOT", os.path.join(_real_home(), ".hermes"))
    sock_path = os.path.join(root, "hermes-bus.sock")
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

    # CLI mode: inject Source line once so agents know their endpoint.
    # (Gateway injects its own via session.py, no-op here.)
    global _source_line_injected
    if not _source_line_injected and os.environ.get("_HERMES_GATEWAY") != "1":
        _source_line_injected = True
        ep = os.environ.get("HERMES_BUS_PLUGIN_ENDPOINT", "hermes-bus")
        msgs.insert(0, f"**Source:** CLI (endpoint: {ep})")

    if not msgs:
        return None

    return {"context": "\n".join(msgs)}


def on_post_tool_call(tool_name: str = "", result: str = "", **kwargs):
    pass


def listen_bus(endpoint: str = "hermes-bus"):
    """Background thread: connect, register, listen."""
    root = os.environ.get("HERMES_BUS_ROOT", os.path.join(_real_home(), ".hermes"))
    sock_path = os.path.join(root, "hermes-bus.sock")
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
                        _banner_print("Set endpoint name in bus-rules.yaml → bus.endpoint, or HERMES_BUS_ENDPOINT env var\n")
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
