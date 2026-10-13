import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from openscn.client import HTTPClient, Workspace, import_git
from openscn.errors import SCNError
from openscn.server import Server
from openscn.store import Store


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'server')
        self.server = Server(('127.0.0.1', 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.environment = patch.dict(os.environ, {'OPENSCN_TOKEN': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def change(self, parents=(), files=None, message='test'):
        changes = {p: None if value is None else {'oid': self.store.put_blob(value), 'executable': False} for p, value in (files or {}).items()}
        return self.store.create_changeset(list(parents), changes, message, 'tester')

    def seed(self, files):
        changeset = self.change(files=files)
        self.store.update_ref('main', changeset['id'], None)
        return changeset

    def test_empty_commit_land_and_stale_ref(self):
        first = Workspace.clone(self.url, self.root / 'first')
        second = Workspace.clone(self.url, self.root / 'second')
        self.assertIsNone(first.metadata['head'])
        (first.root / 'hello').write_text('hello\n')
        os.chmod(first.root / 'hello', 0o755)
        committed = first.commit('first', 'tester')
        self.assertEqual(committed['parents'], [])
        self.assertEqual(self.store.refs(), {})
        first.land()
        (second.root / 'other').write_text('other')
        second.commit('second', 'tester')
        with self.assertRaises(SCNError) as caught:
            second.land()
        self.assertEqual(caught.exception.status, 409)
        second.integrate('rebase', 'main', 'rebase', 'tester')
        second.land()
        first.update()
        self.assertEqual(first.status(), [])
        self.assertTrue((first.root / 'hello').stat().st_mode & 0o111)
        self.assertEqual((first.root / 'other').read_text(), 'other')

    def test_sparse_commit_keeps_unfetched_paths_and_tree_cache_is_verified(self):
        initial = self.seed({'src/a': b'a', 'docs/hidden': b'large hidden file'})
        workspace = Workspace.clone(self.url, self.root / 'work', sparse=['src'])
        self.assertFalse((workspace.root / 'docs').exists())
        self.assertEqual(set(workspace.metadata['files']), {'src/a'})
        root_entries = self.store.get_tree(initial['root'])
        self.assertFalse((workspace.client.cache_dir / ('tree-' + root_entries['docs']['oid'])).exists())
        (workspace.root / 'src/a').write_text('new')
        (workspace.root / 'unselected').write_text('not committed')
        committed = workspace.commit('sparse', 'tester')
        self.assertEqual(set(committed['changes']), {'src/a'})
        self.assertIn('docs/hidden', self.store.manifest(committed['id']))
        oid = self.store.manifest(initial['id'])['src/a']['oid']
        cache = workspace.client.cache_dir / ('blob-' + oid)
        cache.write_bytes(b'corrupt')
        self.assertEqual(workspace.client.get_blob(oid), b'a')

    def test_update_protects_dirty_files_and_untracked_collisions(self):
        first = self.seed({'tracked': b'old'})
        workspace = Workspace.clone(self.url, self.root / 'work')
        next_change = self.change([first['id']], {'tracked': b'new', 'added': b'server'})
        self.store.update_ref('main', next_change['id'], first['id'])
        (workspace.root / 'tracked').write_text('local')
        with self.assertRaisesRegex(SCNError, 'local changes'):
            workspace.update()
        self.assertEqual((workspace.root / 'tracked').read_text(), 'local')
        (workspace.root / 'tracked').write_text('old')
        (workspace.root / 'added').write_text('local untracked')
        with self.assertRaisesRegex(SCNError, 'Untracked'):
            workspace.update()
        self.assertEqual((workspace.root / 'tracked').read_text(), 'old')
        (workspace.root / 'added').unlink()
        (workspace.root / 'keep').write_text('untracked but safe')
        workspace.update()
        self.assertEqual((workspace.root / 'keep').read_text(), 'untracked but safe')
        self.assertEqual((workspace.root / 'tracked').read_text(), 'new')

    def test_symlinks_and_reserved_paths_are_rejected(self):
        initial = self.seed({'a': b'a'})
        workspace = Workspace.clone(self.url, self.root / 'work')
        outside = self.root / 'outside'
        outside.mkdir()
        (workspace.root / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SCNError, 'Symlink'):
            workspace.status()
        self.assertFalse((outside / 'a').exists())
        (workspace.root / 'link').unlink()
        workspace.client.cache_dir.rename(workspace.root / '.openscn' / 'old-cache')
        workspace.client.cache_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SCNError, 'symlink'):
            Workspace.open(workspace.root)

    def test_file_directory_transitions(self):
        initial = self.seed({'x': b'file'})
        workspace = Workspace.clone(self.url, self.root / 'work')
        directory = self.change([initial['id']], {'x': None, 'x/y': b'nested'})
        workspace.update(directory['id'])
        self.assertEqual((workspace.root / 'x/y').read_bytes(), b'nested')
        file_again = self.change([directory['id']], {'x/y': None, 'x': b'file again'})
        workspace.update(file_again['id'])
        self.assertEqual((workspace.root / 'x').read_bytes(), b'file again')
        self.assertEqual(workspace.status(), [])

    def test_cache_byte_limit_and_no_stored_token(self):
        first = self.seed({'a': b'a' * 10, 'b': b'b' * 10})
        client = HTTPClient(self.url, self.root / 'cache', max_cache_bytes=10)
        manifest = self.store.manifest(first['id'])
        for entry in manifest.values():
            self.assertEqual(len(client.get_blob(entry['oid'])), 10)
        self.assertLessEqual(sum(p.stat().st_size for p in client.cache_dir.iterdir()), 10)
        self.server.token = 'secret-token'
        with patch.dict(os.environ, {'OPENSCN_TOKEN': 'secret-token'}):
            workspace = Workspace.clone(self.url, self.root / 'work')
        self.assertNotIn('secret-token', (workspace.root / '.openscn/workspace.json').read_text())
        with self.assertRaises(SCNError) as caught:
            workspace.client.refs()
        self.assertEqual(caught.exception.status, 401)

    def test_failed_fetch_does_not_modify_worktree(self):
        first = self.seed({'a': b'old'})
        workspace = Workspace.clone(self.url, self.root / 'work')
        changed = self.change([first['id']], {'a': None, 'b': b'new'})
        with patch.object(workspace.client, 'get_blob', side_effect=SCNError('network down')):
            with self.assertRaisesRegex(SCNError, 'network down'):
                workspace.update(changed['id'])
        self.assertEqual((workspace.root / 'a').read_bytes(), b'old')
        self.assertFalse((workspace.root / 'b').exists())
        self.assertEqual(workspace.metadata['head'], first['id'])

    def test_clone_unlanded_id_and_unknown_revision(self):
        unlanded = self.change(files={'a': b'unlanded'})
        workspace = Workspace.clone(self.url, self.root / 'work', rev=unlanded['id'])
        self.assertEqual((workspace.root / 'a').read_bytes(), b'unlanded')
        self.assertEqual(workspace.metadata['ref'], 'main')
        with self.assertRaises(SCNError):
            Workspace.clone(self.url, self.root / 'missing', rev='missing')
        self.assertFalse((self.root / 'missing').exists())

    def test_ignore_untracked_paths_but_keep_tracked_changes(self):
        self.seed({'secret.env': b'already tracked'})
        workspace = Workspace.clone(self.url, self.root / 'work')
        (workspace.root / '.openscnignore').write_text('# ignore secrets\n*.env\nbuild/\ncache/*.bin\n')
        (workspace.root / 'secret.env').write_text('tracked edit')
        (workspace.root / 'new.env').write_text('private')
        (workspace.root / 'build').mkdir()
        (workspace.root / 'build/private').write_text('private')
        (workspace.root / 'cache').mkdir()
        (workspace.root / 'cache/a.bin').write_text('private')
        (workspace.root / 'cache/keep').write_text('public')
        changes = workspace.commit('ignore test', 'tester')
        self.assertEqual(set(changes['changes']), {'.openscnignore', 'secret.env', 'cache/keep'})
        self.assertEqual(workspace.status(), [])
        (workspace.root / '.openscnignore').write_text('!unsupported\n')
        with self.assertRaisesRegex(SCNError, 'negation'):
            workspace.status()

    def test_checkout_rejects_case_colliding_paths(self):
        initial = self.seed({'safe': b'original'})
        workspace = Workspace.clone(self.url, self.root / 'work')
        collided = self.change([initial['id']], {'Folder/a': b'a', 'folder/b': b'b'})
        with self.assertRaisesRegex(SCNError, 'collision'):
            workspace.update(collided['id'])
        self.assertEqual(workspace.metadata['head'], initial['id'])
        self.assertFalse((workspace.root / 'Folder').exists())

    @unittest.skipUnless(shutil.which('git'), 'git required')
    def test_git_import_preserves_merge_ancestry_modes_and_deletions(self):
        repo = self.root / 'git'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        git('init', '-b', 'main')
        git('config', 'user.name', 'Example')
        git('config', 'user.email', 'example@example.com')
        (repo / 'base').write_text('base')
        git('add', '.')
        git('commit', '-m', 'base')
        git('checkout', '-b', 'side')
        (repo / 'run').write_text('#!/bin/sh\necho hi\n')
        os.chmod(repo / 'run', 0o755)
        git('add', '.')
        git('commit', '-m', 'side')
        git('checkout', 'main')
        git('rm', 'base')
        git('commit', '-m', 'delete base')
        git('merge', '--no-ff', 'side', '-m', 'merge side')
        result = import_git(repo, self.url)
        self.assertEqual(result['commits'], 4)
        tip = self.store.get_changeset('main')
        self.assertEqual(len(tip['parents']), 2)
        self.assertEqual(len(self.store.log('main')), 3)
        self.assertEqual(set(self.store.manifest('main')), {'run'})
        self.assertTrue(self.store.manifest('main')['run']['executable'])
        self.assertEqual(tip['author'], 'Example <example@example.com>')
        (repo / 'link').symlink_to('run')
        git('add', '.')
        git('commit', '-m', 'symlink')
        with self.assertRaisesRegex(SCNError, 'symlinks and submodules'):
            import_git(repo, self.url)
        self.assertEqual(self.store.resolve('main'), tip['id'])


if __name__ == '__main__':
    unittest.main()
