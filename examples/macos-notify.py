#!/usr/bin/env python3
"""Example: macOS notification via osascript.
Save this script and reference it in notify.yaml:

  - match_type: task_done
    command: python3 /path/to/examples/macos-notify.py
"""

import os, json, subprocess, sys

msg = json.loads(sys.stdin.read())
text = msg.get("body", {}).get("text", "No message")
subprocess.run(["osascript", "-e", f'display notification "{text}" with title "Bus Notification"'])
