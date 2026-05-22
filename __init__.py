"""Hermes bus plugin — entry point for Hermes plugin loader."""
import sys
import os

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_pkg_dir = os.path.join(_plugin_dir, "hermes_bus_plugin")
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from hermes_bus_plugin import register
