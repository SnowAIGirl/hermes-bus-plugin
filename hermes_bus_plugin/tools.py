"""Bus message tools — send, status, info."""

import os as _os


def _real_home() -> str:
    """Get real user home directory, immune to sandbox $HOME overrides."""
    try:
        import pwd
        return pwd.getpwuid(_os.getuid()).pw_dir
    except Exception:
        return _os.path.expanduser("~")


BUS_SEND = {
    "name": "bus_send",
    "description": (
        "Send a message through the Hermes message bus. "
        "Messages are delivered to registered endpoints (services, agents). "
        "Use for cross-process notifications, progress updates, and alerts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Target endpoint name (e.g. 'my-service', 'bot', 'cli')",
            },
            "type": {
                "type": "string",
                "description": "Message type — matched against notify.yaml match_type rules",
            },
            "text": {
                "type": "string",
                "description": "Message body text",
            },
            "channel": {
                "type": "string",
                "description": "Push channel (e.g. 'weixin:<chat_id>') — routes through Gateway adapter",
            },
        },
        "required": ["target", "type", "text"],
    },
}

BUS_STATUS = {
    "name": "bus_status",
    "description": "Check the Hermes message bus status — running endpoints, socket health.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def handle_bus_send(_tool_args: dict | None = None, **kwargs) -> str:
    """Send a message via the bus."""
    if _tool_args is None:
        _tool_args = {}
    target = _tool_args.get("target", "")
    msg_type = _tool_args.get("type", "")
    text = _tool_args.get("text", "")
    channel = _tool_args.get("channel", "")
    import subprocess
    cmd = ["notify-hermes", "--to", target, "--type", msg_type]
    if channel:
        cmd.extend(["--channel", channel])
    cmd.append(text)
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return f"Failed: {result.stderr.strip() or result.stdout.strip()}"
    return f"Sent {msg_type} to {target}: {text[:60]}"


def handle_bus_status(_tool_args: dict | None = None, **kwargs) -> str:
    """Check if bus socket exists and list registered endpoints."""
    import os, json, socket, struct
    root = os.environ.get("HERMES_BUS_ROOT", os.path.join(_real_home(), ".hermes"))
    sock_path = os.path.join(root, "hermes-bus.sock")
    if not os.path.exists(sock_path):
        return "Bus socket not found (not running)"

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(sock_path)
        msg = json.dumps({"type": "list_endpoints"}).encode()
        s.sendall(struct.pack(">I", len(msg)) + msg)
        header = s.recv(4)
        if not header:
            return "Bus: no response"
        length = struct.unpack(">I", header)[0]
        data = s.recv(length).decode()
        s.close()
        resp = json.loads(data)
        endpoints = resp.get("endpoints", {})
        if endpoints:
            ep_list = ", ".join(f"{ep} (sid={sid[:8]}...)" for ep, sid in sorted(endpoints.items()))
            return f"Bus running. Endpoints ({len(endpoints)}): {ep_list}"
        return "Bus running. No connected endpoints."
    except Exception as e:
        return f"Bus error: {e}"


BUS_INFO = {
    "name": "bus_info",
    "description": (
        "Show Hermes Bus connection status — current endpoint, session ID, "
        "registered endpoints, and socket health."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def handle_bus_info(_tool_args: dict | None = None, **kwargs) -> str:
    """Show current bus connection info."""
    import os, json, socket, struct
    root = os.environ.get("HERMES_BUS_ROOT", os.path.join(_real_home(), ".hermes"))
    sock_path = os.path.join(root, "hermes-bus.sock")

    endpoint = os.environ.get("HERMES_BUS_PLUGIN_ENDPOINT", "not set")
    sid = os.environ.get("HERMES_BUS_PLUGIN_SID", "")

    lines = [f"Endpoint: {endpoint}"]
    if sid:
        lines.append(f"Session ID: {sid}")

    if not os.path.exists(sock_path):
        lines.append("Socket: not found (bus not running)")
        return "\n".join(lines)

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(sock_path)
        msg = json.dumps({"type": "list_endpoints"}).encode()
        s.sendall(struct.pack(">I", len(msg)) + msg)
        header = s.recv(4)
        if not header:
            return "\n".join(lines + ["Socket: connected but no response"])
        length = struct.unpack(">I", header)[0]
        data = s.recv(length).decode()
        s.close()
        resp = json.loads(data)
        endpoints = resp.get("endpoints", {})
        lines.append(f"Socket: connected")
        if endpoints:
            lines.append(f"Registered endpoints ({len(endpoints)}):")
            for ep, sid_val in sorted(endpoints.items()):
                lines.append(f"  {ep} (sid={sid_val[:8]}...)")
        else:
            lines.append("Registered endpoints: (none)")
    except Exception as e:
        lines.append(f"Socket: error ({e})")

    return "\n".join(lines)
