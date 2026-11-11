import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cli_backup.cli import main
from cli_backup.config import read_project_env


class CliTests(unittest.TestCase):
    def test_project_file_and_arguments_override_globals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('CLI_BACKUP_PROJECT=project\nCLI_BACKUP_LOCAL_ROOT="/tmp/backups folder"\n')
            output = io.StringIO()
            with patch.dict('os.environ', {'CLI_BACKUP_PROJECT': 'global'}, clear=True), contextlib.redirect_stdout(output):
                self.assertEqual(main(['--project-env', str(path), '--project', 'argument', 'config-check']), 0)
            self.assertIn('argument', output.getvalue())

    def test_publish_local_checks_both_copies_before_manifest(self):
        from cli_backup.storage import LocalStorage, make_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'data'
            source.write_bytes(b'content')
            manifest = make_manifest(source, 'project', 'mysql', 'id.bin', '2026-11-10T00:00:00Z')
            LocalStorage(root / 'local').put(source, manifest)
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(root / 'local'),
                   'CLI_BACKUP_S3_BUCKET': 'bucket'}
            with patch.dict('os.environ', env, clear=True), patch('boto3.client'), patch('cli_backup.cli.S3Storage') as s3, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                remote = s3.return_value
                remote.verify.return_value = False
                self.assertEqual(main(['publish-local', 'mysql', '--file', 'id.bin']), 2)
                remote.client.put_object.assert_not_called()
                remote.verify.return_value = True
                self.assertEqual(main(['publish-local', 'mysql', '--file', 'id.bin']), 0)
                self.assertEqual(remote.client.put_object.call_args.kwargs['IfNoneMatch'], '*')

    def test_project_file_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('CLI_BACKUP_SECRET="$(touch dangerous)"\n')
            self.assertEqual(read_project_env(path)['CLI_BACKUP_SECRET'], '$(touch dangerous)')
            self.assertFalse(Path('dangerous').exists())

    def test_invalid_config_exits_without_secrets(self):
        err = io.StringIO()
        with patch.dict('os.environ', {'CLI_BACKUP_SECRET': 'never-print'}, clear=True), contextlib.redirect_stderr(err):
            self.assertEqual(main(['config-check']), 2)
        self.assertNotIn('never-print', err.getvalue())

    def test_file_backup_list_verify_and_restore(self):
        import re
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'keep').write_text('important')
            (source / 'exclude.tmp').write_text('omit')
            out = io.StringIO()
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups')}
            with patch.dict('os.environ', env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'files', '--directory', str(source), '--exclude', '*.tmp']), 0)
                name = re.search(r'local: (\S+)', out.getvalue()).group(1)
                self.assertEqual(main(['list', 'files']), 0)
                self.assertEqual(main(['verify', 'files', '--file', name]), 0)
                self.assertEqual(main(['restore', 'files', '--file', name, '--destination', str(base / 'restored')]), 0)
            self.assertEqual((base / 'restored/keep').read_text(), 'important')
            self.assertFalse((base / 'restored/exclude.tmp').exists())

    def test_tampered_backup_not_restored(self):
        import re
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'file').write_text('content')
            output = io.StringIO()
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups')}
            with patch.dict('os.environ', env, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'files', '--directory', str(source)]), 0)
                name = re.search(r'local: (\S+)', output.getvalue()).group(1)
                (base / 'backups/project/files' / name).write_bytes(b'wrong')
                self.assertEqual(main(['restore', 'files', '--file', name, '--destination', str(base / 'restore')]), 2)
            self.assertFalse((base / 'restore').exists())

    def test_empty_database_dump_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': tmp,
                   'CLI_BACKUP_DB_NAME': 'fixture', 'CLI_BACKUP_DB_USER': 'user'}
            with patch.dict('os.environ', env, clear=True), patch('cli_backup.cli.capture_mysql'), patch('cli_backup.cli.count_mysql_objects', return_value='1\t0\t0\t0\t0'), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'mysql']), 2)
            self.assertFalse((Path(tmp) / 'project').exists())

    def test_mysql_rehearsal_uses_saved_counts_and_rejects_schema_change_during_dump(self):
        import re
        from cli_backup.storage import LocalStorage
        with tempfile.TemporaryDirectory() as tmp:
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': tmp,
                   'CLI_BACKUP_DB_NAME': 'fixture', 'CLI_BACKUP_DB_USER': 'user',
                   'CLI_BACKUP_TEST_MYSQL_IMAGE': 'fixture/image'}
            out = io.StringIO()
            def capture(_config, output):
                output.write(b'fixture dump')
            with patch.dict('os.environ', env, clear=True), patch('cli_backup.cli.capture_mysql', side_effect=capture), patch('cli_backup.cli.count_mysql_objects', side_effect=['1\t0\t0\t0\t0', '1\t0\t0\t0\t0']), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'mysql']), 0)
            name = re.search(r'local: (\S+)', out.getvalue()).group(1)
            self.assertEqual(LocalStorage(Path(tmp)).list('project', 'mysql')[0].object_counts, '1\t0\t0\t0\t0')
            with patch.dict('os.environ', env, clear=True), patch('cli_backup.cli.rehearse_mysql', return_value='1\t0\t0\t0\t0'), patch('cli_backup.cli.count_mysql_objects', side_effect=AssertionError('must not query changed live schema')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['restore-test', 'mysql', '--file', name]), 0)
            with patch.dict('os.environ', env, clear=True), patch('cli_backup.cli.capture_mysql', side_effect=capture), patch('cli_backup.cli.count_mysql_objects', side_effect=['1\t0\t0\t0\t0', '2\t0\t0\t0\t0']), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'mysql']), 2)
            self.assertEqual(len(LocalStorage(Path(tmp)).list('project', 'mysql')), 1)

    def test_failed_required_s3_does_not_prune_local_backups(self):
        from cli_backup.storage import LocalStorage, make_manifest
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'file').write_text('data')
            old_file = base / 'old'
            old_file.write_bytes(b'old')
            local = LocalStorage(base / 'backups')
            old = make_manifest(old_file, 'project', 'files', 'old.bin', '2000-01-01T00:00:00Z')
            local.put(old_file, old)
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups'),
                   'CLI_BACKUP_LOCAL_DAYS': '1', 'CLI_BACKUP_S3_BUCKET': 'bucket',
                   'CLI_BACKUP_S3_KEY': 'key', 'CLI_BACKUP_S3_SECRET': 'secret'}
            with patch.dict('os.environ', env, clear=True), patch('boto3.client'), patch('cli_backup.cli.S3Storage') as s3, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                s3.return_value.put.side_effect = OSError('upload failure')
                self.assertEqual(main(['backup', 'files', '--directory', str(source)]), 1)
            self.assertIn(old, local.list('project', 'files'))

    def test_download_is_private_and_does_not_replace_existing_file(self):
        import re
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'file').write_text('secret')
            out = io.StringIO()
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups')}
            with patch.dict('os.environ', env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'files', '--directory', str(source)]), 0)
                name = re.search(r'local: (\S+)', out.getvalue()).group(1)
                dest = base / 'download'
                import os
                previous = os.umask(0)
                try:
                    self.assertEqual(main(['download', 'files', '--file', name, '--destination', str(dest)]), 0)
                finally:
                    os.umask(previous)
                self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
                original = dest.read_bytes()
                self.assertEqual(main(['download', 'files', '--file', name, '--destination', str(dest)]), 2)
                self.assertEqual(dest.read_bytes(), original)

    def test_local_list_does_not_initialize_configured_s3(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': tmp,
                   'CLI_BACKUP_S3_BUCKET': 'bucket'}
            with patch.dict('os.environ', env, clear=True), patch('boto3.client', side_effect=RuntimeError('S3 unavailable')) as client, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['list', 'mysql', '--storage', 'local']), 0)
                client.assert_not_called()

    def test_s3_client_setup_failure_still_writes_local_backup(self):
        from cli_backup.storage import LocalStorage
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'file').write_text('data')
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups'),
                   'CLI_BACKUP_S3_BUCKET': 'bucket'}
            with patch.dict('os.environ', env, clear=True), patch('boto3.client', side_effect=RuntimeError('S3 config failed')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'files', '--directory', str(source)]), 1)
            self.assertEqual(len(LocalStorage(base / 'backups').list('project', 'files')), 1)

    def test_unsafe_source_never_publishes_a_successful_backup(self):
        from cli_backup.storage import LocalStorage
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'link').symlink_to('/outside')
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups')}
            with patch.dict('os.environ', env, clear=True), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['backup', 'files', '--directory', str(source)]), 2)
            self.assertEqual(LocalStorage(base / 'backups').list('project', 'files'), [])

    def test_temporary_root_is_configurable_and_command_has_priority(self):
        from unittest.mock import wraps
        from tempfile import TemporaryDirectory
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'file').write_text('data')
            env_tmp = base / 'env-tmp'
            arg_tmp = base / 'arg-tmp'
            env_tmp.mkdir()
            arg_tmp.mkdir()
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_LOCAL_ROOT': str(base / 'backups'),
                   'CLI_BACKUP_TEMP_ROOT': str(env_tmp)}
            with patch.dict('os.environ', env, clear=True), patch('cli_backup.cli.tempfile.TemporaryDirectory', wraps=TemporaryDirectory) as factory, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--temp-root', str(arg_tmp), 'backup', 'files', '--directory', str(source)]), 0)
            self.assertEqual(factory.call_args.kwargs['dir'], str(arg_tmp))
            self.assertEqual(list(arg_tmp.iterdir()), [])

    def test_s3_only_backup_does_not_require_local_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            source.mkdir()
            (source / 'file').write_text('data')
            env = {'CLI_BACKUP_PROJECT': 'project', 'CLI_BACKUP_S3_BUCKET': 'bucket',
                   'CLI_BACKUP_S3_KEY': 'fixture-key', 'CLI_BACKUP_S3_SECRET': 'fixture-secret'}
            with patch.dict('os.environ', env, clear=True), patch('boto3.client'), patch('cli_backup.cli.S3Storage') as s3, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['backup', 'files', '--directory', str(source)]), 0)
                s3.return_value.put.assert_called_once()
