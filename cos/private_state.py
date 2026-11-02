"""Gateway client for state held by a person's own Linux account.

Callers must bind `user` to the authenticated request or a verified background job.
The account registry and OS account comment must agree; request data never selects an account.
"""
from __future__ import annotations

import json
import pwd
import re
import subprocess

from . import accounts, people

WORKER = '/usr/local/bin/neo-private-state'
ACCOUNT = re.compile(r'^neo-[a-z][a-z0-9-]{0,22}$')
DOCUMENTS = frozenset({'memory', 'settings', 'tokens', 'watches', 'findings'})

class StateError(RuntimeError):
    pass

class Conflict(StateError):
    pass


def _request(user: str, request: dict) -> dict:
    if not people.valid_user(user) or people.role(user) == 'blocked':
        raise PermissionError('invalid or blocked user')
    if request.get('document') not in DOCUMENTS:
        raise ValueError('unknown private state document')
    name = accounts.everyone().get(user)
    if name is None:
        # Provision using the immutable Slack id; loading their display name would recurse into
        # their as-yet-unavailable private settings. The caller must already have authenticated user.
        name = accounts.ensure(user, user)
    if not isinstance(name, str) or not ACCOUNT.fullmatch(name):
        raise PermissionError('no verified account for user')
    try:
        record = pwd.getpwnam(name)
    except KeyError:
        raise PermissionError('no verified account for user') from None
    if record.pw_gecos != f'neo {user}':
        raise PermissionError('account identity mismatch')
    try:
        result = subprocess.run(['sudo', '-n', '-u', name, WORKER], input=json.dumps(request),
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateError('private state worker unavailable') from exc
    if result.returncode:
        try:
            error = json.loads(result.stderr)
        except (ValueError, TypeError):
            error = {}
        if error.get('error') == 'Conflict':
            raise Conflict('state changed; read again')
        raise StateError('private state worker failed')
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise StateError('invalid private state response') from exc
    if not isinstance(data, dict) or 'value' not in data or not isinstance(data.get('revision'), str):
        raise StateError('invalid private state response')
    return data


def read(user: str, document: str) -> dict:
    return _request(user, {'op': 'read', 'document': document})


def write(user: str, document: str, value: object, revision: str) -> dict:
    return _request(user, {'op': 'write', 'document': document, 'value': value, 'revision': revision})
