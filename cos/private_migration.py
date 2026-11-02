"""Copy legacy personal memory to user-owned state. Call only while writers are stopped.

The legacy file remains intact for verification and rollback. A different target is never overwritten.
"""
from __future__ import annotations

import os
import json
import stat
import tempfile
from pathlib import Path

from . import learn, people, private_state


def copy_personal_memory(user: str) -> int:
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('migration must run before private storage is enabled')
    if not people.valid_user(user):
        raise ValueError('invalid user')
    path = learn.rules_file(user)
    with learn._locked():
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = b''
        lines = original.decode('utf-8').splitlines()
        if any(line.strip() and not line.lstrip().startswith('#') and not learn._ENTRY_RE.match(line.strip()) for line in lines):
            raise ValueError('legacy memory contains non-entry text; preserve it with manual migration')
        rows = learn.entries(path)
        value = {'entries': rows}
        existing = private_state.read(user, 'memory')
        if existing['value'] is None:
            private_state.write(user, 'memory', value, existing['revision'])
        elif existing['value'] != value:
            raise ValueError('private memory already exists with different content')
        try:
            after = path.read_bytes()
        except FileNotFoundError:
            after = b''
        if after != original or private_state.read(user, 'memory')['value'] != value:
            raise RuntimeError('memory changed during migration; keep legacy files and retry safely')
        return len(rows)


def copy_personal_settings(user: str) -> int:
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('migration must run before private storage is enabled')
    if not people.valid_user(user):
        raise ValueError('invalid user')
    path = people._path(user)
    with people._lock:
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = b'{}'
        data = json.loads(original)
        if not isinstance(data, dict) or set(data) - set(people.FIELDS):
            raise ValueError('legacy settings have unknown fields; manual review required')
        role = data.get('role')
        if role is not None and role not in people.ROLES:
            raise ValueError('invalid trusted role')
        value = {k: v for k, v in data.items() if k != 'role'}
        existing = private_state.read(user, 'settings')
        if existing['value'] is None:
            private_state.write(user, 'settings', value, existing['revision'])
        elif existing['value'] != value:
            raise ValueError('private settings already exist with different content')
        try:
            after = path.read_bytes()
        except FileNotFoundError:
            after = b'{}'
        if after != original or private_state.read(user, 'settings')['value'] != value:
            raise RuntimeError('settings changed during migration; keep legacy files and retry safely')
        return len(value)


def finalize_personal_settings(user: str, backup_dir: Path) -> None:
    """After a verified copy, keep only service-controlled role policy in the legacy file."""
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('cutover must run while private storage is disabled')
    if not people.valid_user(user):
        raise ValueError('invalid user')
    path = people._path(user)
    backup_dir = Path(backup_dir)
    with people._lock:
        original = path.read_bytes()
        data = json.loads(original)
        if not isinstance(data, dict) or set(data) - set(people.FIELDS):
            raise ValueError('legacy settings have unknown fields')
        if 'role' in data and data['role'] not in people.ROLES:
            raise ValueError('invalid trusted role')
        value = {k: v for k, v in data.items() if k != 'role'}
        if private_state.read(user, 'settings')['value'] != value:
            raise ValueError('private settings differ from legacy source')
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = backup_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('backup directory must be private and owned by this account')
        backup = backup_dir / f'{user}.json'
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise FileExistsError('backup already exists; review before retrying') from None
        with os.fdopen(fd, 'wb') as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != original:
            raise RuntimeError('settings changed during cutover; legacy file retained')
        fd, tmp = tempfile.mkstemp(prefix='.policy-', dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump({'role': data['role']} if 'role' in data else {}, out)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def rollback_personal_settings(user: str, backup_dir: Path) -> None:
    """Restore settings only if neither private preferences nor trusted role changed after cutover."""
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('rollback must run while private storage is disabled')
    if not people.valid_user(user):
        raise ValueError('invalid user')
    backup_dir = Path(backup_dir)
    info = backup_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError('backup directory must be private and owned by this account')
    path = people._path(user)
    with people._lock:
        fd = os.open(backup_dir / f'{user}.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError('backup must be private and owned by this account')
            original = source.read(1024 * 1024 + 1)
        if len(original) > 1024 * 1024:
            raise ValueError('backup is too large')
        data = json.loads(original)
        if not isinstance(data, dict) or set(data) - set(people.FIELDS):
            raise ValueError('invalid settings backup')
        if 'role' in data and data['role'] not in people.ROLES:
            raise ValueError('invalid backed-up role')
        if private_state.read(user, 'settings')['value'] != {k: v for k, v in data.items() if k != 'role'}:
            raise ValueError('private settings changed after cutover; reconcile before rollback')
        expected_policy = {'role': data['role']} if 'role' in data else {}
        if json.loads(path.read_bytes()) != expected_policy:
            raise ValueError('trusted role changed after cutover; reconcile before rollback')
        fd, tmp = tempfile.mkstemp(prefix='.rollback-', dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(original)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def copy_personal_tokens(user: str) -> int:
    from . import tokens
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('migration must run before private storage is enabled')
    if not people.valid_user(user):
        raise ValueError('invalid user')
    with tokens.STORE.lock:
        path = tokens.STORE.path()
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = b'{}'
        data = json.loads(original)
        if not isinstance(data, dict) or (user in data and not isinstance(data[user], dict)):
            raise ValueError('invalid legacy token store')
        value = data.get(user, {})
        if any(k not in tokens.TOOLS or not isinstance(v, str) or not tokens._TOKEN.fullmatch(v)
               for k, v in value.items()):
            raise ValueError('invalid legacy token entry')
        existing = private_state.read(user, 'tokens')
        if existing['value'] is None:
            private_state.write(user, 'tokens', value, existing['revision'])
        elif existing['value'] != value:
            raise ValueError('private tokens already exist with different content')
        try:
            after = path.read_bytes()
        except FileNotFoundError:
            after = b'{}'
        if after != original or private_state.read(user, 'tokens')['value'] != value:
            raise RuntimeError('token store changed during migration; source retained')
        return len(value)


def finalize_personal_memory(user: str, backup_dir: Path) -> None:
    """Remove the legacy copy only after the private copy and a rollback backup are verified."""
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('cutover must run while private storage is disabled')
    if not people.valid_user(user):
        raise ValueError('invalid user')
    path = learn.rules_file(user)
    backup_dir = Path(backup_dir)
    with learn._locked():
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('legacy memory is not a regular file')
        original = path.read_bytes()
        if any(line.strip() and not line.lstrip().startswith('#') and not learn._ENTRY_RE.match(line.strip())
               for line in original.decode('utf-8').splitlines()):
            raise ValueError('legacy memory contains non-entry text; manual review required')
        if private_state.read(user, 'memory')['value'] != {'entries': learn.entries(path)}:
            raise ValueError('private memory differs from legacy source')
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = backup_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('backup directory must be private and owned by this account')
        backup = backup_dir / f'{user}.md'
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != original:
            raise RuntimeError('memory changed during cutover; legacy file retained')
        path.unlink()


def finalize_personal_tokens(backup_dir: Path) -> None:
    """Remove the shared token copies only if every user's private copy matches."""
    from . import tokens
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('cutover must run while private storage is disabled')
    path = tokens.STORE.path()
    backup_dir = Path(backup_dir)
    with tokens.STORE.lock:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('legacy token store is not a regular file')
        original = path.read_bytes()
        data = json.loads(original)
        if not isinstance(data, dict):
            raise ValueError('invalid legacy token store')
        for user, value in data.items():
            if not people.valid_user(user) or not isinstance(value, dict):
                raise ValueError('invalid token owner')
            if any(k not in tokens.TOOLS or not isinstance(v, str) or not tokens._TOKEN.fullmatch(v)
                   for k, v in value.items()):
                raise ValueError('invalid legacy token entry')
            if private_state.read(user, 'tokens')['value'] != value:
                raise ValueError('private tokens differ from legacy source')
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = backup_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('backup directory must be private and owned by this account')
        fd = os.open(backup_dir / 'tokens.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != original:
            raise RuntimeError('token store changed during cutover; legacy file retained')
        fd, tmp = tempfile.mkstemp(prefix='.tokens-', dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write('{}\n')
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def partition_watches(watches: list, findings: dict, owner: str) -> tuple[dict, list]:
    """Plan a lossless partition and record uncertain ownership assigned to the owner."""
    if not isinstance(owner, str) or not people.valid_user(owner) or not isinstance(watches, list) or not isinstance(findings, dict):
        raise ValueError('invalid watch migration input')
    partitions, fallbacks = {}, []
    by_watch = {}
    def group(user):
        return partitions.setdefault(user, {'watches': [], 'findings': {}})
    for watch in watches:
        if not isinstance(watch, dict):
            raise ValueError('invalid watch')
        wid = watch.get('id')
        user = watch.get('owner') or owner
        if not isinstance(wid, str) or not wid or wid.startswith('system:') or wid in by_watch:
            raise ValueError('invalid or duplicate watch identity')
        if not isinstance(user, str) or not people.valid_user(user):
            user = owner
            fallbacks.append({'id': wid, 'kind': 'watch', 'reason': 'invalid owner identity'})
        by_watch[wid] = user
        group(user)['watches'].append(watch)
    for fid, finding in findings.items():
        if not isinstance(fid, str) or not isinstance(finding, dict) or finding.get('id') != fid:
            raise ValueError('invalid finding identity')
        wid = finding.get('watch_id')
        if isinstance(wid, str) and wid.startswith('system:') and len(wid) > len('system:'):
            user = owner
        else:
            user = by_watch.get(wid) if isinstance(wid, str) else None
        if not user:
            user = owner
            fallbacks.append({'id': fid, 'kind': 'finding', 'reason': 'missing watch owner'})
        group(user)['findings'][fid] = finding
    return partitions, fallbacks


def copy_watch_partition(user: str, partition: dict) -> tuple[int, int]:
    if os.environ.get('NEO_PRIVATE_STATE') == '1':
        raise RuntimeError('migration must run before private storage is enabled')
    if not people.valid_user(user) or not isinstance(partition, dict):
        raise ValueError('invalid watch partition')
    watches, findings = partition.get('watches'), partition.get('findings')
    if not isinstance(watches, list) or not isinstance(findings, dict):
        raise ValueError('invalid watch partition')
    values = {'watches': watches, 'findings': findings}
    existing = {name: private_state.read(user, name) for name in values}
    if any(existing[name]['value'] is not None and existing[name]['value'] != value
           for name, value in values.items()):
        raise ValueError('private watch data already exists with different content')
    for name, value in values.items():
        if existing[name]['value'] is None:
            private_state.write(user, name, value, existing[name]['revision'])
        if private_state.read(user, name)['value'] != value:
            raise RuntimeError('private watch copy verification failed')
    return len(watches), len(findings)


def partition_runs(runs: list, registry: dict, fallback_owner: str) -> tuple[dict, list]:
    """Assign ambiguous records to the explicitly chosen owner and report each fallback."""
    from .runner import valid_run_id
    if not isinstance(runs, list) or not isinstance(registry, dict):
        raise ValueError('invalid run migration input')
    reverse = {}
    for user, account in registry.items():
        if not isinstance(user, str) or not people.valid_user(user) or not isinstance(account, str) or not private_state.ACCOUNT.fullmatch(account) or account in reverse:
            raise ValueError('invalid or duplicate account mapping')
        reverse[account] = user
    if not isinstance(fallback_owner, str) or not people.valid_user(fallback_owner) or fallback_owner not in registry:
        raise ValueError('fallback owner must have a verified account mapping')
    owners, fallbacks, seen = {}, [], set()
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get('id'), str) or not valid_run_id(run['id']) or run['id'] in seen:
            raise ValueError('invalid or duplicate run identity')
        seen.add(run['id'])
        user, account = run.get('slack_user'), run.get('account')
        account_user = reverse.get(account) if isinstance(account, str) else None
        reason = None
        if user and (not isinstance(user, str) or not people.valid_user(user)):
            reason = 'invalid user identity'
        elif account and not account_user:
            reason = 'unknown account'
        elif user and account_user and user != account_user:
            reason = 'conflicting user and account'
        elif not user and not account_user:
            reason = 'missing user and account'
        if reason:
            fallbacks.append({'id': run['id'], 'reason': reason})
            user = fallback_owner
        else:
            user = user or account_user
        owners.setdefault(user, []).append(run)
    return owners, fallbacks


def write_fallback_audit(path: Path, owner: str, fallbacks: list) -> None:
    """Persist the ownership decision without copying private record contents into the manifest."""
    if not people.valid_user(owner) or not isinstance(fallbacks, list):
        raise ValueError('invalid fallback audit')
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
           or not isinstance(item.get('reason'), str) or set(item) - {'id', 'reason', 'kind'}
           for item in fallbacks):
        raise ValueError('invalid fallback audit record')
    path = Path(path)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError('audit directory must be private and owned by this account')
    data = json.dumps({'owner': owner, 'fallbacks': fallbacks}, indent=2).encode() + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
