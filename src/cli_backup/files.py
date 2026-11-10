"""Full directory archives and guarded POSIX extraction."""
from __future__ import annotations

import fnmatch
import os
import posixpath
import shutil
import stat
import tarfile
from contextlib import contextmanager
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Iterator, BinaryIO


def _validate_links(links: dict[str, str]) -> None:
    """Resolve every archive link as the kernel would, without leaving its root."""
    for name, target in links.items():
        if not target or target.startswith('/'):
            raise ValueError(f'unsafe archive link: {name}')
        parent = PurePosixPath(name).parent
        stack = [] if str(parent) == '.' else list(parent.parts)
        pending = deque(target.split('/'))
        traversals = 0
        while pending:
            part = pending.popleft()
            if not part or part == '.':
                continue
            if part == '..':
                if not stack:
                    raise ValueError(f'unsafe archive link: {name}')
                stack.pop()
                continue
            stack.append(part)
            candidate = '/'.join(stack)
            if candidate in links:
                traversals += 1
                if traversals > 40:
                    raise ValueError(f'cyclic archive link: {name}')
                next_target = links[candidate]
                if not next_target or next_target.startswith('/'):
                    raise ValueError(f'unsafe archive link: {name}')
                stack.pop()
                pending.extendleft(reversed(next_target.split('/')))


def capture_directory(path: Path, patterns: list[str], output: Path) -> None:
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('source must be a directory')
    if output.resolve().is_relative_to(root):
        raise ValueError('archive must be outside source')
    def excluded(name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    def walk_error(error: OSError) -> None:
        raise error
    # tarfile silently skips sockets: reject them and any unsupported metadata
    # before creating an archive that our extractor could not restore.
    source_links: dict[str, str] = {}
    for current, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        kept_dirs = []
        for name in directories + filenames:
            item = Path(current) / name
            relative = item.relative_to(root).as_posix()
            if excluded(relative):
                continue
            mode = item.lstat().st_mode
            if stat.S_ISLNK(mode):
                source_links[relative] = os.readlink(item)
            elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f'unsupported source file type: {relative}')
            if name in directories:
                kept_dirs.append(name)
        directories[:] = kept_dirs
    _validate_links(source_links)
    archived_links: dict[str, str] = {}
    def include(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = info.name.removeprefix('./')
        if excluded(name):
            return None
        if info.islnk():
            # Preserve file contents without creating archive hard links.
            info.type = tarfile.REGTYPE
            info.size = (root / name).stat(follow_symlinks=False).st_size
            info.linkname = ''
        if info.issym():
            archived_links[posixpath.normpath(name)] = info.linkname
        elif not (info.isfile() or info.isdir()):
            raise ValueError(f'unsupported source file type: {name}')
        return info
    with tarfile.open(output, 'x:gz', dereference=False) as archive:
        archive.add(root, arcname='.', recursive=True, filter=include)
        _validate_links(archived_links)


@contextmanager
def open_archive(path: Path) -> Iterator[BinaryIO]:
    """Open an absolute regular file by no-follow descriptor traversal."""
    text = os.fspath(path)
    if not text.startswith('/') or text.startswith('//') or '\x00' in text or '//' in text:
        raise ValueError('archive path must be an absolute local path')
    parts = text.split('/')[1:]
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise ValueError('invalid archive path')
    if not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('safe archive opening requires O_NOFOLLOW')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        archive_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(archive_fd).st_mode):
                raise ValueError('archive must be a regular file')
            with os.fdopen(archive_fd, 'rb', closefd=False) as stream:
                yield stream
        finally:
            os.close(archive_fd)
    finally:
        os.close(fd)


def extract_archive(artifact: Path, destination: Path) -> None:
    """Extract into a new directory, using no-follow file descriptors throughout."""
    if not destination.is_absolute() or destination.name in ('', '.', '..'):
        raise ValueError('destination must be an absolute new directory')
    with open_archive(artifact) as stream, tarfile.open(fileobj=stream, mode='r:gz') as archive:
        members = archive.getmembers()
        names: set[str] = set()
        links: dict[str, str] = {}
        for item in members:
            raw = item.name
            name = posixpath.normpath(raw)
            if raw.startswith('/') or '..' in PurePosixPath(raw).parts or name == '..' or name.startswith('../') or not (item.isfile() or item.isdir() or item.issym()):
                raise ValueError('unsafe archive member')
            if name in names:
                raise ValueError('duplicate archive member')
            names.add(name)
            if item.issym():
                links[name] = item.linkname
        _validate_links(links)
        for name in names:
            if any(str(parent) in links for parent in PurePosixPath(name).parents):
                raise ValueError('member beneath symlink')

        parent_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in destination.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child
            os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
            root_fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            def parent_for(name: str) -> tuple[int, str]:
                parts = PurePosixPath(name).parts
                fd = os.dup(root_fd)
                try:
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, 0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        os.close(fd)
                        fd = child
                    return fd, parts[-1]
                except Exception:
                    os.close(fd)
                    raise

            directories: list[tuple[str, int]] = []
            for item in members:
                name = posixpath.normpath(item.name)
                if name == '.':
                    if not item.isdir():
                        raise ValueError('invalid archive root')
                    continue
                fd, basename = parent_for(name)
                try:
                    if item.isdir():
                        try:
                            os.mkdir(basename, 0o700, dir_fd=fd)
                        except FileExistsError:
                            check = os.open(basename, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            os.close(check)
                        directories.append((name, item.mode & 0o777))
                    elif item.issym():
                        os.symlink(item.linkname, basename, dir_fd=fd)
                    else:
                        target = os.open(basename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                        with os.fdopen(target, 'wb') as out:
                            source = archive.extractfile(item)
                            if source is None:
                                raise ValueError('cannot read archive member')
                            with source:
                                shutil.copyfileobj(source, out)
                            os.fchmod(out.fileno(), item.mode & 0o777)
                finally:
                    os.close(fd)
            for name, mode in reversed(directories):
                fd, basename = parent_for(name)
                try:
                    child = os.open(basename, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        os.fchmod(child, mode)
                    finally:
                        os.close(child)
                finally:
                    os.close(fd)
        finally:
            os.close(root_fd)
