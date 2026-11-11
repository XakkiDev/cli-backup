import io
import tempfile
import unittest
from pathlib import Path
from cli_backup.storage import LocalStorage, S3Storage, make_manifest


class FakeS3:
    def __init__(self): self.data = {}
    def upload_file(self, path, bucket, key): self.data[bucket, key] = Path(path).read_bytes()
    def get_object(self, *, Bucket, Key): return {'Body': io.BytesIO(self.data[Bucket, Key])}
    def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
        if (Bucket, Key) in self.data: raise FileExistsError(Key)
        self.data[Bucket, Key] = Body.read() if hasattr(Body, 'read') else Body


class StorageTests(unittest.TestCase):
    def test_local_and_s3_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            path.write_bytes(b'backup')
            manifest = make_manifest(path, 'project', 'mysql', 'id.bin', '2026-11-10T00:00:00Z')
            local = LocalStorage(Path(tmp) / 'store')
            local.put(path, manifest)
            self.assertEqual(local.list('project', 'mysql'), [manifest])
            self.assertTrue(local.verify(manifest))
            s3 = FakeS3()
            remote = S3Storage(s3, 'bucket')
            remote.put(path, manifest)
            self.assertTrue(remote.verify(manifest))
            with self.assertRaises(FileExistsError):
                remote.put(path, manifest)
            s3.data['bucket', 'project/mysql/id.bin'] = b'corrupt'
            self.assertFalse(remote.verify(manifest))

    def test_large_s3_reads_retry_a_failed_range_and_verify_checksum(self):
        from unittest.mock import patch
        from cli_backup.storage import s3_get
        class Ranged(FakeS3):
            def __init__(self):
                super().__init__()
                self.failed = False
            def get_object(self, *, Bucket, Key, Range):
                start, end = map(int, Range.removeprefix('bytes=').split('-'))
                if start == 3 and not self.failed:
                    self.failed = True
                    raise OSError('transient read failure')
                data = self.data[Bucket, Key]
                return {'Body': io.BytesIO(data[start:end + 1]),
                        'ContentRange': f'bytes {start}-{end}/{len(data)}'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            path.write_bytes(b'abcdefghi')
            manifest = make_manifest(path, 'project', 'mysql', 'id.bin', '2026-11-10T00:00:00Z')
            client = Ranged()
            client.data['bucket', 'project/mysql/id.bin'] = path.read_bytes()
            store = S3Storage(client, 'bucket')
            with patch('cli_backup.storage.MULTIPART_THRESHOLD', 2), patch('cli_backup.storage.S3_RANGE_SIZE', 3):
                output = io.BytesIO()
                s3_get(store, manifest, output)
                self.assertEqual(output.getvalue(), b'abcdefghi')
                self.assertTrue(client.failed)
                self.assertTrue(store.verify(manifest))
                client.data['bucket', 'project/mysql/id.bin'] = b'xbcdefghi'
                self.assertFalse(store.verify(manifest))

    def test_incomplete_is_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'project' / 'mysql'
            folder.mkdir(parents=True)
            (folder / 'orphan').write_bytes(b'incomplete')
            (folder / 'bad.json').write_bytes(b'{')
            self.assertEqual(LocalStorage(Path(tmp)).list('project', 'mysql'), [])

    def test_verified_download_rejects_corruption(self):
        from cli_backup.storage import local_get
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            path.write_bytes(b'original')
            manifest = make_manifest(path, 'project', 'mysql', 'id.bin', '2026-11-10T00:00:00Z')
            store = LocalStorage(Path(tmp) / 'store')
            store.put(path, manifest)
            (store.directory(manifest) / manifest.artifact).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                local_get(store, manifest, io.BytesIO())

    def test_retention_removes_only_expired_project(self):
        from datetime import datetime, timezone
        from cli_backup.storage import prune_local
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'source'
            path.write_bytes(b'x')
            store = LocalStorage(root / 'store')
            old = make_manifest(path, 'project', 'mysql', 'old.bin', '2020-01-01T00:00:00+00:00')
            other = make_manifest(path, 'other', 'mysql', 'old.bin', '2020-01-01T00:00:00+00:00')
            store.put(path, old)
            store.put(path, other)
            self.assertEqual(prune_local(store, 'project', 'mysql', 3, datetime(2026, 1, 1, tzinfo=timezone.utc)), 1)
            self.assertEqual(store.list('other', 'mysql'), [other])
            with self.assertRaises(ValueError):
                prune_local(store, 'project', 'mysql', 0, datetime.now(timezone.utc))

    def test_retention_refuses_symlinked_project_directory(self):
        from datetime import datetime, timezone
        from cli_backup.storage import prune_local
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'unrelated').write_text('keep')
            store_root = root / 'store'
            store_root.mkdir()
            (store_root / 'project').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                prune_local(LocalStorage(store_root), 'project', 'mysql', 3, datetime.now(timezone.utc))
            self.assertEqual((outside / 'unrelated').read_text(), 'keep')

    def test_multipart_failure_aborts_upload(self):
        class Multipart:
            def __init__(self): self.aborted = False
            def create_multipart_upload(self, **kwargs): return {'UploadId': 'temporary'}
            def upload_part(self, **kwargs): raise OSError('injected failure')
            def abort_multipart_upload(self, **kwargs): self.aborted = True
        client = Multipart()
        store = S3Storage(client, 'bucket')
        with self.assertRaises(OSError):
            store._multipart_upload(io.BytesIO(b'part'), 'project/mysql/id')
        self.assertTrue(client.aborted)

    def test_private_copy_does_not_publish_partial_file(self):
        from cli_backup.storage import write_private_copy
        class BrokenStream:
            def __init__(self): self.calls = 0
            def read(self, size):
                self.calls += 1
                if self.calls == 1: return b'partial'
                raise OSError('disk error')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(OSError):
                write_private_copy(BrokenStream(), root / 'download')
            self.assertEqual(list(root.iterdir()), [])

    def test_private_copy_refuses_parent_replacement_race(self):
        import os
        from unittest.mock import patch
        from cli_backup.storage import write_private_copy
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / 'parent'
            parent.mkdir(mode=0o700)
            original_link = os.link
            def replace_parent(*args, **kwargs):
                parent.rename(base / 'moved')
                parent.mkdir()
                return original_link(*args, **kwargs)
            with patch('cli_backup.storage.os.link', side_effect=replace_parent):
                with self.assertRaises(OSError):
                    write_private_copy(io.BytesIO(b'secret'), parent / 'download')
            self.assertEqual(list(parent.iterdir()), [])
            self.assertEqual(list((base / 'moved').iterdir()), [])

    def test_invalid_manifest_types_cannot_break_listing(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'project' / 'mysql'
            folder.mkdir(parents=True)
            (folder / 'bad.bin').write_bytes(b'x')
            (folder / 'bad.bin.json').write_text(json.dumps({
                'version': True, 'project': 'project', 'source': 'mysql',
                'artifact': 'bad.bin', 'timestamp': None, 'size': True, 'sha256': 'a' * 64,
            }))
            self.assertEqual(LocalStorage(root).list('project', 'mysql'), [])

    def test_list_orders_by_instant_not_timestamp_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.write_bytes(b'data')
            store = LocalStorage(base / 'backup')
            later = make_manifest(source, 'project', 'mysql', 'later.bin', '2026-01-01T00:00:00.100000+00:00')
            earlier = make_manifest(source, 'project', 'mysql', 'earlier.bin', '2026-01-01T00:00:00Z')
            store.put(source, later)
            store.put(source, earlier)
            self.assertEqual(store.list('project', 'mysql'), [earlier, later])

    def test_private_copy_rejects_untrusted_writable_parent(self):
        from cli_backup.storage import write_private_copy
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / 'shared'
            parent.mkdir(mode=0o777)
            parent.chmod(0o777)
            with self.assertRaises(OSError):
                write_private_copy(io.BytesIO(b'secret'), parent / 'download')
            self.assertEqual(list(parent.iterdir()), [])

    def test_s3_interrupted_upload_and_checksum_failure_never_publish_manifest(self):
        class Interrupted(FakeS3):
            def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
                if not Key.endswith('.json'):
                    self.data[Bucket, Key] = Body.read(2)
                    raise OSError('interrupted upload')
                return super().put_object(Bucket=Bucket, Key=Key, Body=Body, IfNoneMatch=IfNoneMatch)
        class Corrupted(FakeS3):
            def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
                if not Key.endswith('.json'):
                    self.data[Bucket, Key] = b'corrupted'
                    return
                return super().put_object(Bucket=Bucket, Key=Key, Body=Body, IfNoneMatch=IfNoneMatch)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            path.write_bytes(b'complete backup')
            manifest = make_manifest(path, 'project', 'mysql', 'id.bin', '2026-11-10T00:00:00Z')
            for client in (Interrupted(), Corrupted()):
                with self.subTest(client=type(client).__name__), self.assertRaises((OSError, ValueError)):
                    S3Storage(client, 'bucket').put(path, manifest)
                self.assertNotIn(('bucket', 'project/mysql/id.bin.json'), client.data)
