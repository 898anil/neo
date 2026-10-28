"""Test package for unittest discovery."""

import os

import dotenv

# Tests never read this checkout's .env (app.py loads it on import): on a live host it turns on per-person
# accounts and lent logins, which the tests set up for themselves when they want them.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")  # app.py needs one to import

# Tests never read this Mac's launchd jobs (cos/system_watches.py); test_system_watches turns it back on.
os.environ["NEO_SYSTEM_WATCHES"] = "0"
