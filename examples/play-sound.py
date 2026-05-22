#!/usr/bin/env python3
"""Example: play a sound file via afplay (macOS).
Save this script and reference it in notify.yaml:

  - match_type: task_done
    command: python3 /path/to/examples/play-sound.py
"""

import os, json, subprocess, sys, random

msg = json.loads(sys.stdin.read())
sound_dir = os.path.expanduser("~/.hermes/sounds")
os.makedirs(sound_dir, exist_ok=True)

files = [f for f in os.listdir(sound_dir) if f.endswith((".mp3", ".wav", ".m4a"))]
if files:
    path = os.path.join(sound_dir, random.choice(files))
    subprocess.Popen(["afplay", path])
