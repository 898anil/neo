"""User-owned state worker: compare-and-swap writes and safe filesystem access."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def worker():
    loader = importlib.machinery.SourceFileLoader('neo_private_state', str(ROOT / 'deploy/linux/neo-private-state'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

class PrivateStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.mod = worker()

    def test_read_write_and_compare_and_swap(self):
        store = self.mod.PrivateState(self.home)
        before = store.read('memory')
        self.assertIsNone(before['value'])
        result = store.write('memory', {'rules': ['hello']}, before['revision'])
        self.assertEqual(store.read('memory'), result)
        with self.assertRaises(self.mod.Conflict):
            store.write('memory', {'rules': ['lost']}, before['revision'])
        self.assertEqual(store.read('memory')['value'], {'rules': ['hello']})

    def test_rejects_unrecognized_names_and_symlinked_state(self):
        store = self.mod.PrivateState(self.home)
        for bad in ('../memory', 'other-tokens', 'runs/../../x', ''):
            with self.assertRaises(ValueError):
                store.read(bad)
        (self.home / '.neo-private').symlink_to(self.home / 'outside')
        with self.assertRaises(OSError):
            store.read('memory')

    def test_fails_closed_on_corrupt_data(self):
        store = self.mod.PrivateState(self.home)
        result = store.write('settings', {'engine': 'claude'}, store.read('settings')['revision'])
        self.assertIsNotNone(result['value'])
        (self.home / '.neo-private' / 'settings.json').write_text('{bad')
        with self.assertRaises(ValueError):
            store.read('settings')

    def test_competing_updates_cannot_silently_overwrite_each_other(self):
        store = self.mod.PrivateState(self.home)
        revision = store.read('memory')['revision']
        def update(value):
            try:
                store.write('memory', value, revision)
                return 'written'
            except self.mod.Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ['first', 'second']))
        self.assertCountEqual(outcomes, ['written', 'conflict'])
        self.assertIn(store.read('memory')['value'], ['first', 'second'])

    def test_refuses_symlink_to_another_file(self):
        store = self.mod.PrivateState(self.home)
        store.read('memory')
        secret = self.home / 'secret'
        secret.write_text('"secret"')
        (self.home / '.neo-private' / 'memory.json').symlink_to(secret)
        with self.assertRaises(OSError):
            store.read('memory')
        self.assertEqual(secret.read_text(), '"secret"')

    def test_fifo_state_fails_without_blocking_worker(self):
        store = self.mod.PrivateState(self.home)
        store.read('memory')
        os.mkfifo(self.home / '.neo-private' / 'memory.json')
        script = '''import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader('worker', sys.argv[1])
spec = importlib.util.spec_from_loader('worker', loader)
m = importlib.util.module_from_spec(spec); loader.exec_module(m)
try: m.PrivateState(sys.argv[2]).read('memory')
except ValueError: sys.exit(0)
sys.exit(1)
'''
        try:
            proc = subprocess.run([sys.executable, '-c', script, str(ROOT / 'deploy/linux/neo-private-state'), str(self.home)], timeout=1)
        except subprocess.TimeoutExpired:
            self.fail('worker blocked reading a named pipe')
        self.assertEqual(proc.returncode, 0)

class PrivateStateBrokerTest(unittest.TestCase):
    def test_broker_binds_user_to_registered_account_and_passes_data_on_stdin(self):
        from unittest.mock import patch, Mock
        from types import SimpleNamespace
        from cos import private_state
        proc = SimpleNamespace(returncode=0, stdout='{"value":null,"revision":"missing"}', stderr='')
        account = SimpleNamespace(pw_name='neo-ajay', pw_gecos='neo UAJAY01')
        with patch('cos.accounts.everyone', return_value={'UAJAY01': 'neo-ajay'}), \
             patch('cos.private_state.pwd.getpwnam', return_value=account), \
             patch('cos.private_state.subprocess.run', return_value=proc) as call:
            result = private_state.read('UAJAY01', 'memory')
        self.assertIsNone(result['value'])
        args, kwargs = call.call_args
        self.assertEqual(args[0], ['sudo', '-n', '-u', 'neo-ajay', '/usr/local/bin/neo-private-state'])
        self.assertNotIn('UAJAY01', args[0])
        self.assertIn('"document": "memory"', kwargs['input'])

    def test_broker_refuses_missing_or_mismatched_account(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from cos import private_state
        with patch('cos.accounts.everyone', return_value={'UAJAY01': 'neo-ajay'}), \
             patch('cos.private_state.pwd.getpwnam', return_value=SimpleNamespace(pw_gecos='neo UYOSH01')):
            with self.assertRaises(PermissionError):
                private_state.read('UAJAY01', 'memory')
        with patch('cos.accounts.everyone', return_value={}), \
             patch('cos.accounts.ensure', side_effect=PermissionError('unavailable')):
            with self.assertRaises(PermissionError):
                private_state.read('UAJAY01', 'memory')

class PrivateMemoryIntegrationTest(unittest.TestCase):
    def test_personal_memory_uses_user_worker_without_falling_back_to_shared_files(self):
        from unittest.mock import patch
        from cos import learn
        states = {}
        def read(user, doc):
            return {'value': states.get(user), 'revision': str(len((states.get(user) or {}).get('entries', [])))}
        def write(user, doc, value, revision):
            if revision != read(user, doc)['revision']:
                raise AssertionError('unexpected revision')
            states[user] = value
            return read(user, doc)
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_MEMORY_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '1'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            learn.add('Ajay prefers short replies', 'rule', user='UAJAY01')
            learn.add('Yosh prefers examples', 'rule', user='UYOSH01')
            self.assertIn('Ajay prefers short replies', learn.brief(None, user='UAJAY01'))
            self.assertNotIn('Yosh prefers examples', learn.brief(None, user='UAJAY01'))
            self.assertFalse((Path(home) / 'people' / 'UAJAY01.md').exists())
            files = learn.all_files(user='UAJAY01', everyone=False)
            self.assertTrue(any(e['text'] == 'Ajay prefers short replies' for f in files for e in f['entries']))

    def test_unavailable_private_memory_never_falls_back_to_shared_file(self):
        from unittest.mock import patch
        from cos import learn, private_state
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_MEMORY_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '1'}), \
             patch('cos.private_state.read', side_effect=private_state.StateError('unavailable')):
            path = Path(home) / 'people' / 'UAJAY01.md'
            path.parent.mkdir()
            path.write_text('- stale, should not be read\n')
            with self.assertRaises(private_state.StateError):
                learn.entries(path)

class MemoryMigrationTest(unittest.TestCase):
    def test_migration_preserves_entries_and_is_idempotent(self):
        from unittest.mock import patch
        from cos import private_migration
        state = {'value': None, 'revision': 'missing'}
        def read(user, doc): return dict(state)
        def write(user, doc, value, revision):
            self.assertEqual(revision, state['revision'])
            state.update(value=value, revision='written')
            return dict(state)
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_MEMORY_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            p = Path(home) / 'people' / 'UAJAY01.md'
            p.parent.mkdir(); p.write_text('# Personal\n\n- Keep PRs small <!-- 2026-01-01 rule -->\n')
            self.assertEqual(private_migration.copy_personal_memory('UAJAY01'), 1)
            self.assertEqual(private_migration.copy_personal_memory('UAJAY01'), 1)
            self.assertEqual(state['value']['entries'][0]['meta'], '2026-01-01 rule')
            self.assertTrue(p.exists())

    def test_migration_refuses_to_overwrite_existing_different_memory(self):
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_MEMORY_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'entries': [{'text': 'existing', 'meta': ''}]}, 'revision': 'r'}), \
             patch('cos.private_state.write') as write:
            p = Path(home) / 'people' / 'UAJAY01.md'
            p.parent.mkdir(); p.write_text('- migrated\n')
            with self.assertRaises(ValueError):
                private_migration.copy_personal_memory('UAJAY01')
            write.assert_not_called()

class PrivateBriefFailureTest(unittest.TestCase):
    def test_private_memory_failure_stops_agent_brief(self):
        from unittest.mock import patch
        from cos import config, private_state
        with patch.dict(os.environ, {'NEO_PRIVATE_STATE': '1'}), \
             patch('cos.learn.brief', side_effect=private_state.StateError('offline')), \
             patch('cos.people.brief', return_value=''):
            with self.assertRaises(private_state.StateError):
                config.agent_brief(None, user='UAJAY01')

class PrivateSettingsIntegrationTest(unittest.TestCase):
    def test_private_preferences_do_not_control_authorization_role(self):
        from unittest.mock import patch
        from cos import people
        state = {}
        def read(user, document):
            return {'value': state.get(user), 'revision': str(len(state))}
        def write(user, document, value, revision):
            state[user] = dict(value)
            return read(user, document)
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '1'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            people.save('UAJAY01', 'UAJAY01', style='short')
            people.save('UYOSH01', 'UYOSH01', style='detailed')
            self.assertEqual(people.load('UAJAY01')['style'], 'short')
            self.assertEqual(people.load('UYOSH01')['style'], 'detailed')
            self.assertFalse((Path(home) / 'UAJAY01.json').exists())
            people.save('UAJAY01', 'UOWNER1', role='read-only')
            state['UAJAY01']['role'] = 'full'
            self.assertEqual(people.role('UAJAY01'), 'read-only')
            self.assertEqual(people.load('UAJAY01')['role'], 'read-only')
            self.assertNotIn('style', (Path(home) / 'UAJAY01.json').read_text())

    def test_unavailable_private_settings_do_not_fall_back_to_legacy(self):
        from unittest.mock import patch
        from cos import people, private_state
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '1'}), \
             patch('cos.private_state.read', side_effect=private_state.StateError('offline')):
            (Path(home) / 'UAJAY01.json').write_text('{"style":"stale"}')
            with self.assertRaises(private_state.StateError):
                people.load('UAJAY01')

    def test_corrupt_policy_never_grants_full_role(self):
        from cos import people
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '1'}):
            path = Path(home) / 'UAJAY01.json'
            for content in ('[]', '{"role":"invalid"}', '{broken'):
                path.write_text(content)
                with self.assertRaises((ValueError, OSError)):
                    people.role('UAJAY01')

class SettingsMigrationTest(unittest.TestCase):
    def test_copies_preferences_without_moving_trusted_role(self):
        from unittest.mock import patch
        from cos import private_migration
        state = {'value': None, 'revision': 'missing'}
        def read(user, doc): return dict(state)
        def write(user, doc, value, revision):
            self.assertEqual(revision, state['revision'])
            state.update(value=value, revision='written')
            return dict(state)
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            p = Path(home) / 'UAJAY01.json'
            p.write_text('{"style":"short","role":"read-only"}')
            self.assertEqual(private_migration.copy_personal_settings('UAJAY01'), 1)
            self.assertEqual(private_migration.copy_personal_settings('UAJAY01'), 1)
            self.assertEqual(state['value'], {'style': 'short'})
            self.assertEqual(p.read_text(), '{"style":"short","role":"read-only"}')

    def test_refuses_existing_private_settings_with_other_values(self):
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'style': 'existing'}, 'revision': 'r'}), \
             patch('cos.private_state.write') as write:
            (Path(home) / 'UAJAY01.json').write_text('{"style":"different"}')
            with self.assertRaises(ValueError):
                private_migration.copy_personal_settings('UAJAY01')
            write.assert_not_called()

class PrivateBootstrapTest(unittest.TestCase):
    def test_authenticated_new_person_is_provisioned_without_loading_their_private_settings(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from cos import private_state
        account = SimpleNamespace(pw_gecos='neo UNEW123')
        proc = SimpleNamespace(returncode=0, stdout='{"value":null,"revision":"missing"}', stderr='')
        with patch('cos.accounts.everyone', return_value={}), \
             patch('cos.accounts.ensure', return_value='neo-unew123') as ensure, \
             patch('cos.private_state.pwd.getpwnam', return_value=account), \
             patch('cos.private_state.subprocess.run', return_value=proc):
            self.assertIsNone(private_state.read('UNEW123', 'memory')['value'])
            ensure.assert_called_once_with('UNEW123', 'UNEW123')

class SettingsCutoverTest(unittest.TestCase):
    def test_removes_legacy_preferences_only_after_verified_copy_and_backup(self):
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'style': 'short'}, 'revision': 'x'}):
            path = Path(home) / 'UAJAY01.json'; original = '{"style":"short","role":"read-only"}'
            path.write_text(original)
            backup = Path(home) / 'backup'
            private_migration.finalize_personal_settings('UAJAY01', backup)
            self.assertEqual(__import__('json').loads(path.read_text()), {'role': 'read-only'})
            self.assertEqual((backup / 'UAJAY01.json').read_text(), original)

    def test_refuses_cutover_when_private_copy_differs(self):
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'style': 'wrong'}, 'revision': 'x'}):
            path = Path(home) / 'UAJAY01.json'; original = '{"style":"short"}'
            path.write_text(original)
            with self.assertRaises(ValueError):
                private_migration.finalize_personal_settings('UAJAY01', Path(home) / 'backup')
            self.assertEqual(path.read_text(), original)

class PrivateTokensIntegrationTest(unittest.TestCase):
    def test_sentry_tokens_are_user_owned_and_user_specific(self):
        from unittest.mock import patch, Mock
        from cos import tokens
        states = {}
        def read(user, doc):
            return {'value': states.get(user), 'revision': str(len(states))}
        def write(user, doc, value, revision):
            states[user] = value
            return read(user, doc)
        with patch.dict(os.environ, {'NEO_PRIVATE_STATE': '1', 'NEO_OWNER_USER': 'UOWNER1'}), \
             patch('cos.tokens.STORE', Mock(read=Mock(side_effect=AssertionError('shared store accessed')),
                                            update=Mock(side_effect=AssertionError('shared store accessed')))), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            tokens.save('UAJAY01', 'sentry', 'ajay-token-123')
            tokens.save('UYOSH01', 'sentry', 'yosh-token-123')
            self.assertEqual(set(states), {'UAJAY01', 'UYOSH01'})
            self.assertEqual(tokens.env_for('UAJAY01'), {'SENTRY_AUTH_TOKEN': 'ajay-token-123'})
            self.assertEqual(tokens.env_for('UYOSH01'), {'SENTRY_AUTH_TOKEN': 'yosh-token-123'})
            self.assertTrue(tokens.remove('UAJAY01', 'sentry'))
            self.assertEqual(tokens.connected('UAJAY01'), set())
            self.assertEqual(tokens.connected('UYOSH01'), {'sentry'})

class TokenMigrationTest(unittest.TestCase):
    def test_copies_only_owners_tokens_without_revealing_or_removing_source(self):
        import json
        from unittest.mock import patch
        from cos import private_migration
        state = {'value': None, 'revision': 'missing'}
        def read(user, doc): return dict(state)
        def write(user, doc, value, revision):
            state.update(value=value, revision='written')
            return dict(state)
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'COS_HOME': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            path = Path(home) / 'tokens.json'
            path.write_text(json.dumps({'UAJAY01': {'sentry': 'ajay-secret-123'}, 'UYOSH01': {'sentry': 'yosh-secret-123'}}))
            self.assertEqual(private_migration.copy_personal_tokens('UAJAY01'), 1)
            self.assertEqual(state['value'], {'sentry': 'ajay-secret-123'})
            self.assertIn('UYOSH01', json.loads(path.read_text()))

class MemoryCutoverTest(unittest.TestCase):
    def test_preserves_backup_and_removes_shared_copy_only_after_verified_private_copy(self):
        from unittest.mock import patch
        from cos import private_migration
        entry = {'text': 'Keep PRs small', 'meta': '2026-01-01 rule'}
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_MEMORY_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'entries': [entry]}, 'revision': 'x'}):
            p = Path(home) / 'people/UAJAY01.md'; p.parent.mkdir(); original = '- Keep PRs small <!-- 2026-01-01 rule -->\n'
            p.write_text(original)
            backup = Path(home) / 'backups'
            private_migration.finalize_personal_memory('UAJAY01', backup)
            self.assertFalse(p.exists())
            self.assertEqual((backup / 'UAJAY01.md').read_text(), original)

    def test_refuses_to_remove_source_when_private_copy_differs(self):
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_MEMORY_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'entries': []}, 'revision': 'x'}):
            p = Path(home) / 'people/UAJAY01.md'; p.parent.mkdir(); p.write_text('- Keep PRs small\n')
            with self.assertRaises(ValueError):
                private_migration.finalize_personal_memory('UAJAY01', Path(home) / 'backups')
            self.assertTrue(p.exists())

class PrivateStateEndToEndTest(unittest.TestCase):
    def test_personal_memory_settings_and_sentry_are_independent_end_to_end(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        from cos import accounts, learn, people, tokens
        mod = worker()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            homes = {name: root / name for name in ('neo-ajay', 'neo-yosh')}
            for home in homes.values(): home.mkdir()
            registry = {'UAJAY01': 'neo-ajay', 'UYOSH01': 'neo-yosh'}
            def pw(name):
                user = next(user for user, account in registry.items() if account == name)
                return SimpleNamespace(pw_gecos=f'neo {user}')
            def run(args, *, input, **kwargs):
                req = json.loads(input)
                store = mod.PrivateState(homes[args[3]])
                if req['op'] == 'read': result = store.read(req['document'])
                else: result = store.write(req['document'], req['value'], req['revision'])
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')
            with patch.dict(os.environ, {'NEO_MEMORY_DIR': str(root / 'shared'),
                                         'NEO_PEOPLE_DIR': str(root / 'policy'),
                                         'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '1'}), \
                 patch.object(accounts, 'everyone', return_value=registry), \
                 patch('cos.private_state.pwd.getpwnam', side_effect=pw), \
                 patch('cos.private_state.subprocess.run', side_effect=run):
                people.save('UAJAY01', 'UAJAY01', style='short')
                people.save('UYOSH01', 'UYOSH01', style='detailed')
                learn.add('ajay memory', 'rule', user='UAJAY01')
                learn.add('yosh memory', 'rule', user='UYOSH01')
                tokens.save('UAJAY01', 'sentry', 'ajay-token-123')
                tokens.save('UYOSH01', 'sentry', 'yosh-token-123')
                self.assertEqual(people.load('UAJAY01')['style'], 'short')
                self.assertEqual(people.load('UYOSH01')['style'], 'detailed')
                self.assertIn('ajay memory', learn.brief(None, user='UAJAY01'))
                self.assertNotIn('yosh memory', learn.brief(None, user='UAJAY01'))
                self.assertEqual(tokens.env_for('UAJAY01'), {'SENTRY_AUTH_TOKEN': 'ajay-token-123'})
                self.assertEqual(tokens.env_for('UYOSH01'), {'SENTRY_AUTH_TOKEN': 'yosh-token-123'})
                self.assertFalse((root / 'shared/people/UAJAY01.md').exists())
                self.assertFalse((root / 'policy/UAJAY01.json').exists())

class TokenCutoverTest(unittest.TestCase):
    def test_cuts_over_only_after_all_users_verified_and_backup_preserved(self):
        import json
        from unittest.mock import patch
        from cos import private_migration
        records = {'UAJAY01': {'sentry': 'ajay-token-123'}, 'UYOSH01': {'sentry': 'yosh-token-123'}}
        def read(user, doc): return {'value': records[user], 'revision': 'ok'}
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'COS_HOME': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', side_effect=read):
            path = Path(home) / 'tokens.json'; path.write_text(json.dumps(records))
            backup = Path(home) / 'backups'
            private_migration.finalize_personal_tokens(backup)
            self.assertEqual(json.loads(path.read_text()), {})
            self.assertEqual(json.loads((backup / 'tokens.json').read_text()), records)

    def test_changed_destination_keeps_legacy_source(self):
        import json
        from unittest.mock import patch
        from cos import private_migration
        records = {'UAJAY01': {'sentry': 'ajay-token-123'}}
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'COS_HOME': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {}, 'revision': 'x'}):
            path = Path(home) / 'tokens.json'; path.write_text(json.dumps(records))
            with self.assertRaises(ValueError): private_migration.finalize_personal_tokens(Path(home) / 'backup')
            self.assertEqual(json.loads(path.read_text()), records)

class PrivateFallbackTest(unittest.TestCase):
    def test_sentry_uses_own_token_then_owner_fallback_for_regular_runs(self):
        from unittest.mock import patch
        from cos import accounts
        values = {'UOWNER1': {'sentry': 'owner-token-123'}, 'UAJAY01': {}}
        def read(user, doc): return {'value': values.get(user), 'revision': 'r'}
        meta = {'cwd': '/tmp', 'account': 'neo-ajay', 'slack_user': 'UAJAY01', 'role': 'full'}
        with patch.dict(os.environ, {'NEO_PRIVATE_STATE': '1', 'NEO_OWNER_USER': 'UOWNER1'}), \
             patch('cos.private_state.read', side_effect=read), \
             patch('cos.logins.connector_status', return_value='missing'), \
             patch.object(accounts, '_shared_doppler', return_value='doppler-owner-token'):
            spec = accounts.spec(meta, ['claude', '-p'], {}, {})
            self.assertEqual(spec['tokens']['SENTRY_AUTH_TOKEN'], 'owner-token-123')
            self.assertEqual(spec['tokens']['DOPPLER_TOKEN'], 'doppler-owner-token')
            values['UAJAY01'] = {'sentry': 'ajay-token-123'}
            spec = accounts.spec(meta, ['claude', '-p'], {}, {})
            self.assertEqual(spec['tokens']['SENTRY_AUTH_TOKEN'], 'ajay-token-123')
            spec = accounts.spec({**meta, 'lend': False}, ['claude', '-p'], {}, {})
            self.assertEqual(spec['tokens']['SENTRY_AUTH_TOKEN'], 'ajay-token-123')
            self.assertNotIn('DOPPLER_TOKEN', spec['tokens'])

class WatchPartitionTest(unittest.TestCase):
    def test_partition_preserves_watches_findings_and_system_owner(self):
        from cos import private_migration
        watches = [{'id': 'one', 'owner': 'UAJAY01', 'enabled': True}, {'id': 'owner', 'enabled': False}]
        findings = {'f1': {'id': 'f1', 'watch_id': 'one', 'status': 'new'},
                    'f2': {'id': 'f2', 'watch_id': 'owner', 'status': 'dismissed'},
                    'f3': {'id': 'f3', 'watch_id': 'system:feed-watch', 'status': 'new'}}
        result, fallback = private_migration.partition_watches(watches, findings, 'UOWNER1')
        self.assertEqual(fallback, [])
        self.assertEqual(result['UAJAY01']['watches'], [watches[0]])
        self.assertEqual(result['UAJAY01']['findings'], {'f1': findings['f1']})
        self.assertEqual(result['UOWNER1']['watches'], [watches[1]])
        self.assertEqual(result['UOWNER1']['findings'], {'f2': findings['f2'], 'f3': findings['f3']})

    def test_orphan_findings_fall_back_to_owner_but_duplicate_ids_block_migration(self):
        from cos import private_migration
        finding = {'id': 'f1', 'watch_id': 'unknown'}
        rows, fallback = private_migration.partition_watches([], {'f1': finding}, 'UOWNER1')
        self.assertEqual(rows['UOWNER1']['findings'], {'f1': finding})
        self.assertEqual(fallback, [{'id': 'f1', 'kind': 'finding', 'reason': 'missing watch owner'}])
        with self.assertRaises(ValueError):
            private_migration.partition_watches([{'id': 'dup'}, {'id': 'dup', 'owner': 'UAJAY01'}], {}, 'UOWNER1')

    def test_invalid_watch_owner_falls_back_and_records_reason(self):
        from cos import private_migration
        watch = {'id': 'x', 'owner': 'bad-user'}
        rows, fallback = private_migration.partition_watches([watch], {}, 'UOWNER1')
        self.assertEqual(rows['UOWNER1']['watches'], [watch])
        self.assertEqual(fallback, [{'id': 'x', 'kind': 'watch', 'reason': 'invalid owner identity'}])

class WatchMigrationTest(unittest.TestCase):
    def test_copies_and_verifies_both_documents_idempotently(self):
        from unittest.mock import patch
        from cos import private_migration
        states = {}
        def read(user, doc): return {'value': states.get((user, doc)), 'revision': 'r' if (user, doc) in states else 'missing'}
        def write(user, doc, value, rev):
            states[user, doc] = value
            return read(user, doc)
        part = {'watches': [{'id': 'one', 'enabled': True}], 'findings': {'f1': {'id': 'f1', 'watch_id': 'one'}}}
        with patch.dict(os.environ, {'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write', side_effect=write):
            self.assertEqual(private_migration.copy_watch_partition('UAJAY01', part), (1, 1))
            self.assertEqual(private_migration.copy_watch_partition('UAJAY01', part), (1, 1))
            self.assertEqual(states['UAJAY01', 'watches'], part['watches'])
            self.assertEqual(states['UAJAY01', 'findings'], part['findings'])

    def test_conflicting_destination_never_overwrites_either_document(self):
        from unittest.mock import patch
        from cos import private_migration
        part = {'watches': [{'id': 'one'}], 'findings': {}}
        def read(user, doc):
            return {'value': None if doc == 'watches' else {'other': {}}, 'revision': 'r'}
        with patch.dict(os.environ, {'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', side_effect=read), patch('cos.private_state.write') as write:
            with self.assertRaises(ValueError): private_migration.copy_watch_partition('UAJAY01', part)
            write.assert_not_called()

class RunPartitionTest(unittest.TestCase):
    def test_ambiguous_runs_fall_back_to_owner_with_audit(self):
        from cos import private_migration
        rows = [
            {'id': '20261101-120000-aaaaaa', 'slack_user': 'UAJAY01', 'source': 'slack'},
            {'id': '20261101-120001-bbbbbb', 'source': 'dashboard', 'account': 'neo-anil'},
            {'id': '20261101-120002-cccccc', 'source': 'slack'},
            {'id': '20261101-120003-dddddd', 'source': 'a2a'},
        ]
        owners, fallback = private_migration.partition_runs(rows, {'UOWNER1': 'neo-anil', 'UAJAY01': 'neo-ajay'}, 'UOWNER1')
        self.assertEqual(owners['UAJAY01'], [rows[0]])
        self.assertEqual(owners['UOWNER1'], rows[1:])
        self.assertEqual([x['id'] for x in fallback], [rows[2]['id'], rows[3]['id']])

    def test_invalid_and_duplicate_run_id_are_rejected(self):
        from cos import private_migration
        with self.assertRaises(ValueError): private_migration.partition_runs([{'id': '../oops'}], {'UOWNER1': 'neo-anil'}, 'UOWNER1')
        with self.assertRaises(ValueError): private_migration.partition_runs([{'id': 123}], {'UOWNER1': 'neo-anil'}, 'UOWNER1')
        run = {'id': '20261101-120000-aaaaaa', 'source': 'schedule'}
        with self.assertRaises(ValueError): private_migration.partition_runs([run, run], {'UOWNER1': 'neo-anil'}, 'UOWNER1')

    def test_conflicting_user_and_account_falls_back_to_owner_with_audit(self):
        from cos import private_migration
        row = {'id': '20261101-120004-eeeeee', 'slack_user': 'UAJAY01', 'account': 'neo-yosh'}
        owners, fallback = private_migration.partition_runs([row], {'UOWNER1': 'neo-anil', 'UAJAY01': 'neo-ajay', 'UYOSH01': 'neo-yosh'}, 'UOWNER1')
        self.assertEqual(owners, {'UOWNER1': [row]})
        self.assertEqual(fallback, [{'id': row['id'], 'reason': 'conflicting user and account'}])

    def test_fallback_owner_must_be_in_verified_registry(self):
        from cos import private_migration
        with self.assertRaises(ValueError):
            private_migration.partition_runs([], {'UAJAY01': 'neo-ajay'}, 'UOWNER1')

    def test_invalid_user_type_falls_back_to_owner(self):
        from cos import private_migration
        row = {'id': '20261101-120005-ffffff', 'slack_user': 42}
        owners, fallback = private_migration.partition_runs([row], {'UOWNER1': 'neo-anil'}, 'UOWNER1')
        self.assertEqual(owners['UOWNER1'], [row])
        self.assertEqual(fallback[0]['reason'], 'invalid user identity')

class SettingsRollbackTest(unittest.TestCase):
    def test_restores_original_only_if_private_and_policy_unchanged(self):
        import json
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'style': 'short'}, 'revision': 'x'}):
            path = Path(home) / 'UAJAY01.json'; path.write_text('{"role":"read-only"}')
            backup = Path(home) / 'backup'; backup.mkdir(mode=0o700)
            original = {'style': 'short', 'role': 'read-only'}
            (backup / 'UAJAY01.json').write_text(json.dumps(original))
            (backup / 'UAJAY01.json').chmod(0o600)
            private_migration.rollback_personal_settings('UAJAY01', backup)
            self.assertEqual(json.loads(path.read_text()), original)

    def test_refuses_rollback_when_private_data_changed_after_cutover(self):
        import json
        from unittest.mock import patch
        from cos import private_migration
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {'NEO_PEOPLE_DIR': home, 'NEO_OWNER_USER': 'UOWNER1', 'NEO_PRIVATE_STATE': '0'}), \
             patch('cos.private_state.read', return_value={'value': {'style': 'new'}, 'revision': 'x'}):
            path = Path(home) / 'UAJAY01.json'; path.write_text('{}')
            backup = Path(home) / 'backup'; backup.mkdir(mode=0o700)
            (backup / 'UAJAY01.json').write_text(json.dumps({'style': 'old'}))
            (backup / 'UAJAY01.json').chmod(0o600)
            with self.assertRaises(ValueError): private_migration.rollback_personal_settings('UAJAY01', backup)
            self.assertEqual(json.loads(path.read_text()), {})

class WorkerInstallationTest(unittest.TestCase):
    def test_installation_limits_worker_to_neo_accounts_and_no_command_arguments(self):
        policy = (ROOT / 'deploy/linux/sudoers').read_text().splitlines()
        lines = [line for line in policy if 'neo-private-state' in line and not line.lstrip().startswith('#')]
        self.assertEqual(lines, ['SVC_USER ALL=(%neo-people) NOPASSWD: /usr/local/bin/neo-private-state ""'])
        installer = (ROOT / 'deploy/install-linux.sh').read_text()
        self.assertIn('install -o root -g root -m 755 deploy/linux/neo-private-state /usr/local/bin/neo-private-state', installer)

class FallbackAuditTest(unittest.TestCase):
    def test_fallback_audit_is_persisted_privately_and_never_overwrites_existing(self):
        import json
        from cos import private_migration
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fallback-audit.json'
            items = [{'id': '20261101-120002-cccccc', 'reason': 'missing user and account'}]
            private_migration.write_fallback_audit(path, 'UOWNER1', items)
            self.assertEqual(json.loads(path.read_text()), {'owner': 'UOWNER1', 'fallbacks': items})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                private_migration.write_fallback_audit(path, 'UOWNER1', [])
            self.assertEqual(json.loads(path.read_text())['fallbacks'], items)
