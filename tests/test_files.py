import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from cli_backup.files import capture_directory, extract_archive


class FileTests(unittest.TestCase):
    def test_roundtrip_and_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'keep').write_text('hello')
            (source / 'skip.tmp').write_text('skip')
            archive = base / 'backup.tar.gz'
            capture_directory(source, ['*.tmp'], archive)
            extract_archive(archive, base / 'restored')
            self.assertEqual((base / 'restored/keep').read_text(), 'hello')
            self.assertFalse((base / 'restored/skip.tmp').exists())

    def test_reject_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                item = tarfile.TarInfo('../escape')
                item.size = 1
                tar.addfile(item, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                extract_archive(archive, Path(tmp) / 'output')
            self.assertFalse((Path(tmp) / 'escape').exists())

    def test_reject_escaping_symlink_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                link = tarfile.TarInfo('link')
                link.type = tarfile.SYMTYPE
                link.linkname = '../escape'
                tar.addfile(link)
            with self.assertRaises(ValueError):
                extract_archive(archive, base / 'out')
            self.assertFalse((base / 'out').exists())
            (base / 'existing').mkdir()
            with self.assertRaises(ValueError):
                extract_archive(archive, base / 'existing')

    def test_reject_symlink_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'real').write_bytes(b'not archive')
            (base / 'link').symlink_to(base / 'real')
            with self.assertRaises(OSError):
                extract_archive(base / 'link', base / 'out')

    def test_restore_modes_symlinks_and_hardlink_contents(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'regular').write_text('value')
            (source / 'regular').chmod(0o640)
            os.link(source / 'regular', source / 'hardlink')
            (source / 'link').symlink_to('regular')
            archive = base / 'backup.tar.gz'
            capture_directory(source, [], archive)
            with tarfile.open(archive, 'r:gz') as tar:
                member = tar.getmember('./regular')
                self.assertEqual((member.uid, member.gid), (os.getuid(), os.getgid()))
            extract_archive(archive, base / 'restored')
            self.assertEqual((base / 'restored/hardlink').read_text(), 'value')
            self.assertEqual((base / 'restored/regular').stat().st_mode & 0o777, 0o640)
            self.assertEqual(os.readlink(base / 'restored/link'), 'regular')

    def test_open_archive_pins_descriptor_and_rejects_symlink_ancestor(self):
        from cli_backup.files import open_archive
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            folder = base / 'dir'
            folder.mkdir()
            path = folder / 'archive'
            path.write_bytes(b'original')
            with open_archive(path) as stream:
                path.rename(folder / 'renamed')
                path.write_bytes(b'replacement')
                self.assertEqual(stream.read(), b'original')
            (base / 'link').symlink_to(folder, target_is_directory=True)
            with self.assertRaises(OSError):
                with open_archive(base / 'link/archive'):
                    pass

    def test_unsafe_or_corrupt_archive_never_creates_destination(self):
        for kind in ('absolute', 'nested_traversal', 'fifo', 'duplicate', 'corrupt'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                archive = base / 'bad.tar.gz'
                if kind == 'corrupt':
                    archive.write_bytes(b'not-gzip')
                else:
                    with tarfile.open(archive, 'w:gz') as tar:
                        item = tarfile.TarInfo({'absolute': '/outside', 'nested_traversal': 'a/../b',
                                               'fifo': 'pipe', 'duplicate': 'same'}[kind])
                        if kind == 'fifo':
                            item.type = tarfile.FIFOTYPE
                        tar.addfile(item)
                        if kind == 'duplicate':
                            tar.addfile(item)
                with self.assertRaises((ValueError, tarfile.TarError)):
                    extract_archive(archive, base / 'out')
                self.assertFalse((base / 'out').exists())

    def test_symlink_chain_cannot_escape_via_parent_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / 'real'
            real.mkdir()
            (real / 'a').symlink_to('.')
            (real / 'b').symlink_to('a/../outside')
            self.assertFalse((real / 'b').resolve().is_relative_to(real))
            archive = base / 'chain.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name, target in [('a', '.'), ('b', 'a/../outside')]:
                    item = tarfile.TarInfo(name)
                    item.type = tarfile.SYMTYPE
                    item.linkname = target
                    tar.addfile(item)
            with self.assertRaises(ValueError):
                extract_archive(archive, base / 'output')
            self.assertFalse((base / 'output').exists())

    def test_capture_rejects_unrestorable_links_and_special_files(self):
        import os
        import socket
        for kind in ('absolute-link', 'parent-link', 'fifo', 'socket'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                source = base / 'source'
                source.mkdir()
                bad = source / 'bad'
                sock = None
                try:
                    if kind == 'absolute-link': bad.symlink_to('/etc/passwd')
                    elif kind == 'parent-link': bad.symlink_to('../other')
                    elif kind == 'fifo': os.mkfifo(bad)
                    else:
                        sock = socket.socket(socket.AF_UNIX)
                        sock.bind(str(bad))
                    archive = base / 'unsafe.tar.gz'
                    with self.assertRaises(ValueError):
                        capture_directory(source, [], archive)
                    self.assertFalse(archive.exists())
                    safe = base / 'safe.tar.gz'
                    capture_directory(source, ['bad'], safe)
                    extract_archive(safe, base / 'restored')
                    self.assertFalse((base / 'restored/bad').exists())
                finally:
                    if sock: sock.close()

    def test_archive_hardlink_is_rejected_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / 'unsafe.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                link = tarfile.TarInfo('hardlink')
                link.type = tarfile.LNKTYPE
                link.linkname = '../outside'
                tar.addfile(link)
            with self.assertRaises(ValueError):
                extract_archive(archive, base / 'restore')
            self.assertFalse((base / 'restore').exists())

    def test_internal_parent_symlink_roundtrip(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            (source / 'sub').mkdir(parents=True)
            (source / 'target').write_text('value')
            (source / 'sub/link').symlink_to('../target')
            archive = base / 'backup.tar.gz'
            capture_directory(source, [], archive)
            extract_archive(archive, base / 'restored')
            self.assertEqual(os.readlink(base / 'restored/sub/link'), '../target')
            self.assertEqual((base / 'restored/sub/link').read_text(), 'value')

    def test_capture_rejects_symlink_chain_that_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'a').symlink_to('.')
            (source / 'b').symlink_to('a/../outside')
            archive = base / 'unsafe.tar.gz'
            with self.assertRaises(ValueError):
                capture_directory(source, [], archive)
            self.assertFalse(archive.exists())

    def test_nested_symlink_chain_cannot_escape_via_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / 'chain.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name, target in [('dir/a', '..'), ('dir/b', 'a/../outside')]:
                    item = tarfile.TarInfo(name)
                    item.type = tarfile.SYMTYPE
                    item.linkname = target
                    tar.addfile(item)
            with self.assertRaises(ValueError):
                extract_archive(archive, base / 'restored')
            self.assertFalse((base / 'restored').exists())

    def test_symlink_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / 'cycle.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name, target in [('a', 'b'), ('b', 'a')]:
                    item = tarfile.TarInfo(name)
                    item.type = tarfile.SYMTYPE
                    item.linkname = target
                    tar.addfile(item)
            with self.assertRaises(ValueError):
                extract_archive(archive, base / 'restored')
            self.assertFalse((base / 'restored').exists())
