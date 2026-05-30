"""Hermes bus plugin — auto-start, auto-register, auto-listen.

Endpoint naming (by priority):
  1. HERMES_BUS_ENDPOINT env var
  2. bus-rules.yaml → bus.endpoint
  3. Profile name derived from HERMES_HOME (default: 'hermes-bus')
  Gateway mode appends '-gateway' suffix.
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


ENV_ENDPOINT = "HERMES_BUS_PLUGIN_ENDPOINT"
ENV_SID = "HERMES_BUS_PLUGIN_SID"


def _get_profile_name() -> str:
    """Extract profile name from HERMES_HOME. Returns 'hermes-bus' for default profile."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    profiles_root = os.path.join(os.path.expanduser("~/.hermes"), "profiles")
    if home.startswith(profiles_root):
        name = os.path.basename(home)
        if name:
            return name
    return "hermes-bus"


def _get_bus_root() -> str:
    """Return the shared bus socket root (always ~/.hermes, not profile-scoped)."""
    return os.environ.get("HERMES_BUS_ROOT", os.path.expanduser("~/.hermes"))


def _read_config_endpoint() -> str:
    """Read bus.endpoint from bus-rules.yaml. Returns '' if not configured."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    config_path = os.path.join(home, "bus-rules.yaml")
    if not os.path.exists(config_path):
        return ""
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        bus = cfg.get("bus", {})
        if isinstance(bus, dict):
            return str(bus.get("endpoint", "")).strip()
    except Exception:
        pass
    return ""


def _default_endpoint() -> str:
    """Return the default bus endpoint name.

    Priority: HERMES_BUS_ENDPOINT env var → bus-rules.yaml bus.endpoint
              → profile name → 'hermes-bus'.

    Uses three signals to detect gateway context, evaluated at call time:
    1. _HERMES_GATEWAY=1 env var (set by run.py:371)
    2. gateway.run in sys.modules (set when run.py starts)
    3. 'gateway' + 'run' in sys.argv (set by CLI before any imports)

    Signal 3 catches the case where main.py:12500 calls discover_plugins()
    BEFORE importing gateway.run — neither signal 1 nor 2 is set yet.

    Must be a function, not a module-level constant.
    """
    cfg_endpoint = os.environ.get("HERMES_BUS_ENDPOINT", "")
    if cfg_endpoint:
        base = cfg_endpoint
    else:
        yaml_endpoint = _read_config_endpoint()
        if yaml_endpoint:
            base = yaml_endpoint
        else:
            base = _get_profile_name()

    is_gateway = (
        os.environ.get("_HERMES_GATEWAY") == "1"
        or "gateway.run" in sys.modules
        or ("gateway" in sys.argv and "run" in sys.argv)
    )
    if is_gateway:
        return f"{base}-gateway"
    return base

from .tools import BUS_SEND, BUS_STATUS, BUS_INFO, handle_bus_send, handle_bus_status, handle_bus_info
from .hooks import (
    on_pre_llm_call,
    on_post_tool_call,
    on_session_start,
    listen_bus,
    ensure_bus_running,
)


def _query_bus_endpoints() -> set:
    """Return set of existing endpoint names from the bus. Empty on failure."""
    root = _get_bus_root()
    sock_path = os.path.join(root, "hermes-bus.sock")
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
    """Build a unique bus endpoint name.

    Priority: HERMES_BUS_ENDPOINT env var → profile default.
    Gateway mode appends '-gateway' suffix automatically.
    Additional sessions get '-2', '-3', etc. to avoid collisions.
    """
    existing = _query_bus_endpoints()
    base = _default_endpoint()

    if base not in existing:
        return base

    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def register(ctx):
    endpoint = _build_endpoint(ctx)
    _log(f"register() endpoint={endpoint}")

    ensure_bus_running()

    t = threading.Thread(
        target=listen_bus, daemon=True, name="bus-listener",
        kwargs={"endpoint": endpoint},
    )
    t.start()

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
