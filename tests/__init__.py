"""Test package for unittest discovery."""

import os

import dotenv

# Tests never read this checkout's .env (app.py loads it on import): on a live host it turns on per-person
# accounts and lent logins, which the tests set up for themselves when they want them.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")  # app.py needs one to import

# Tests never read this Mac's launchd jobs (cos/system_watches.py); test_system_watches turns it back on.
os.environ["NEO_SYSTEM_WATCHES"] = "0"

# Tests never run sudo. On spark it works, and the accounts tests use (neo-ajay, neo-bala...) are real people's:
# a test that forgot a mock would run commands as them, read their logins, even start a real check.
import subprocess  # noqa: E402

_popen = subprocess.Popen


def _no_sudo(args, *rest, **kwargs):
    argv = args if isinstance(args, (list, tuple)) else [args]
    if argv and str(argv[0]).rsplit("/", 1)[-1] == "sudo":
        raise PermissionError(f"tests must not run sudo: {' '.join(map(str, argv))[:120]}")
    return _popen(args, *rest, **kwargs)


subprocess.Popen = _no_sudo
