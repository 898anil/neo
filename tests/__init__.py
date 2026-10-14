"""Test package for unittest discovery."""

import os

# Tests never read this Mac's launchd jobs (cos/system_watches.py); test_system_watches turns it back on.
os.environ["NEO_SYSTEM_WATCHES"] = "0"
