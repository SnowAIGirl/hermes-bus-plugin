"""Hermes bus plugin — auto-start, auto-register, auto-listen.

Endpoint naming:
  1. Hermes session /title value (stable, human-readable)
  2. Query bus for existing endpoints, pick 'hermes-bus' or first available suffix
When /title changes, the bus endpoint re-registers with the new name.
"""

import os
import sys
import threading
import time
import json
import struct
import socket


def _log(msg: str):
    try:
        import datetime
        with open("/tmp/hermes_bus_debug.log", "a") as f:
            f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except Exception:
        pass


_current_title: str = None
_title_lock = threading.Lock()

ENV_ENDPOINT = "HERMES_BUS_PLUGIN_ENDPOINT"
ENV_SID = "HERMES_BUS_PLUGIN_SID"


def _default_endpoint() -> str:
    """Return the default bus endpoint name.

    Uses three signals to detect gateway context, evaluated at call time:
    1. _HERMES_GATEWAY=1 env var (set by run.py:371)
    2. gateway.run in sys.modules (set when run.py starts)
    3. 'gateway' + 'run' in sys.argv (set by CLI before any imports)

    Signal 3 catches the case where main.py:12500 calls discover_plugins()
    BEFORE importing gateway.run — neither signal 1 nor 2 is set yet.

    Must be a function, not a module-level constant.
    """
    if os.environ.get("_HERMES_GATEWAY") == "1":
        return "hermes-bus-gateway"
    if "gateway.run" in sys.modules:
        return "hermes-bus-gateway"
    if "gateway" in sys.argv and "run" in sys.argv:
        return "hermes-bus-gateway"
    return "hermes-bus"

from .tools import BUS_SEND, BUS_STATUS, BUS_INFO, handle_bus_send, handle_bus_status, handle_bus_info
from .hooks import (
    on_pre_llm_call,
    on_post_tool_call,
    on_session_start,
    listen_bus,
    ensure_bus_running,
)


def _get_session_title(ctx) -> str:
    title = getattr(ctx, 'title', None) or getattr(ctx, 'session_title', None)
    if title:
        return str(title).strip()
    title = os.environ.get("HERMES_SESSION_TITLE", "").strip()
    if title:
        return title
    return ""


def _query_bus_endpoints() -> set:
    """Return set of existing endpoint names from the bus. Empty on failure."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    sock_path = os.path.join(home, "hermes-bus.sock")
    if not os.path.exists(sock_path):
        return set()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sock_path)
        msg = json.dumps({"type": "list_endpoints"}).encode()
        s.sendall(struct.pack(">I", len(msg)) + msg)
        header = s.recv(4)
        if not header or len(header) < 4:
            s.close()
            return set()
        length = struct.unpack(">I", header)[0]
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        s.close()
        reply = json.loads(data.decode())
        return set(reply.get("endpoints", {}).keys())
    except Exception:
        return set()


def _build_endpoint(ctx) -> str:
    """Build a unique bus endpoint name."""
    title = _get_session_title(ctx)
    if title:
        return title

    existing = _query_bus_endpoints()
    base = _default_endpoint()

    if base not in existing:
        return base

    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _watch_title(ctx):
    global _current_title
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    sock_path = os.path.join(home, "hermes-bus.sock")

    while True:
        time.sleep(10)
        try:
            title = _get_session_title(ctx)
            with _title_lock:
                if title and title != _current_title:
                    new_endpoint = title
                    if os.path.exists(sock_path):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.settimeout(3)
                        try:
                            s.connect(sock_path)
                            reg = json.dumps({"type": "register", "endpoint": new_endpoint}).encode()
                            s.sendall(struct.pack(">I", len(reg)) + reg)
                            s.close()
                            os.environ[ENV_ENDPOINT] = new_endpoint
                        except Exception:
                            pass
                if title:
                    _current_title = title
        except Exception:
            pass


def register(ctx):
    endpoint = _build_endpoint(ctx)
    _current_title = _get_session_title(ctx)
    _log(f"register() endpoint={endpoint}")

    ensure_bus_running()

    t = threading.Thread(
        target=listen_bus, daemon=True, name="bus-listener",
        kwargs={"endpoint": endpoint},
    )
    t.start()

    tw = threading.Thread(
        target=_watch_title, daemon=True, name="title-watcher",
        args=(ctx,),
    )
    tw.start()

    os.environ[ENV_ENDPOINT] = endpoint

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)

    ctx.register_tool("bus_send", "hermes-bus-plugin", BUS_SEND, handle_bus_send)
    ctx.register_tool("bus_status", "hermes-bus-plugin", BUS_STATUS, handle_bus_status)
    ctx.register_tool("bus_info", "hermes-bus-plugin", BUS_INFO, handle_bus_info)

    # Register notification protocol skill
    import pathlib
    _skill_path = pathlib.Path(__file__).parent.parent / "skills" / "notification-protocol" / "SKILL.md"
    if _skill_path.exists():
        ctx.register_skill("notification-protocol", _skill_path, "Unified notification protocol for bus ecosystem")
