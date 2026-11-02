"""Hermes Agent plugin entry point for TokenEconomizer."""
import os
import sys


_PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from tokeneconomizer.plugin.hermes import register  # noqa: E402
