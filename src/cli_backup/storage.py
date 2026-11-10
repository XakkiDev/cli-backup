"""Manifest-last backup storage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from .config import validate_project

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
MULTIPART_THRESHOLD = 5 * 1024**3
MULTIPART_PART_SIZE = 64 * 1024**2


def valid_name(value: str) -> str:
    if not _NAME.fullmatch(value) or value in (".", ".."):
        raise ValueError("invalid artifact name")
    return value


def digest(stream: BinaryIO) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    while data := stream.read(1024 * 1024):
        size += len(data)
        h.update(data)
    return h.hexdigest(), size


@dataclass(frozen=True)
class Manifest:
    version: int
    project: str
    source: str
    artifact: str
    timestamp: str
    size: int
    sha256: str

    def validate(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported manifest version")
        validate_project(self.project)
        valid_name(self.source)
        valid_name(self.artifact)
        if type(self.size) is not int or self.size < 0 or not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("invalid manifest checksum or size")
        if not isinstance(self.timestamp, str):
            raise ValueError("invalid manifest timestamp")
        try:
            timestamp = datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError("invalid manifest timestamp") from exc
        if timestamp.utcoffset() is None:
            raise ValueError("manifest timestamp must include a timezone")

    def encode(self) -> bytes:
        self.validate()
        return json.dumps(asdict(self), sort_keys=True).encode() + b"\n"

    @classmethod
    def decode(cls, data: bytes) -> Manifest:
        if len(data) > 65536:
            raise ValueError("manifest too large")
        try:
            obj = cls(**json.loads(data))
            obj.validate()
            return obj
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid manifest") from exc


def make_manifest(path: Path, project: str, source: str, name: str, timestamp: str) -> Manifest:
    with path.open("rb") as stream:
        sha, size = digest(stream)
    result = Manifest(1, project, source, name, timestamp, size, sha)
    result.validate()
    return result


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def directory(self, manifest: Manifest) -> Path:
        manifest.validate()
        return self.root / manifest.project / manifest.source

    def put(self, path: Path, manifest: Manifest) -> None:
        folder = self.directory(manifest)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = folder / manifest.artifact
        metadata = folder / (manifest.artifact + ".json")
        if dest.exists() or metadata.exists():
            raise FileExistsError(dest)
        fd, temp = tempfile.mkstemp(prefix=".partial-", dir=folder)
        try:
            with os.fdopen(fd, "wb") as out, path.open("rb") as source:
                shutil.copyfileobj(source, out)
                out.flush()
                os.fsync(out.fileno())
            with open(temp, "rb") as source:
                if digest(source) != (manifest.sha256, manifest.size):
                    raise ValueError("artifact changed during copy")
            os.link(temp, dest)
            meta_fd, meta_temp = tempfile.mkstemp(prefix=".manifest-", dir=folder)
            try:
                with os.fdopen(meta_fd, "wb") as out:
                    out.write(manifest.encode())
                    out.flush()
                    os.fsync(out.fileno())
                os.link(meta_temp, metadata)
            finally:
                os.unlink(meta_temp)
        finally:
            os.unlink(temp)

    def list(self, project: str, source: str) -> list[Manifest]:
        folder = self.root / validate_project(project) / valid_name(source)
        if not folder.exists():
            return []
        manifests = []
        for path in folder.glob("*.json"):
            try:
                m = Manifest.decode(path.read_bytes())
                if m.project == project and m.source == source and path.name == m.artifact + ".json" and (folder / m.artifact).is_file():
                    manifests.append(m)
            except (OSError, ValueError):
                continue
        return sorted(manifests, key=lambda m: datetime.fromisoformat(m.timestamp.replace('Z', '+00:00')))

    def verify(self, manifest: Manifest) -> bool:
        with (self.directory(manifest) / manifest.artifact).open("rb") as stream:
            return digest(stream) == (manifest.sha256, manifest.size)


class S3Storage:
    def __init__(self, client, bucket: str, prefix: str = ""):
        self.client, self.bucket, self.prefix = client, bucket, prefix.strip("/")

    def key(self, manifest: Manifest) -> str:
        manifest.validate()
        return "/".join(part for part in (self.prefix, manifest.project, manifest.source, manifest.artifact) if part)

    def put(self, path: Path, manifest: Manifest) -> None:
        key = self.key(manifest)
        with path.open("rb") as data:
            if digest(data) != (manifest.sha256, manifest.size):
                raise ValueError("artifact changed before upload")
            data.seek(0)
            if manifest.size <= MULTIPART_THRESHOLD:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch="*")
            else:
                self._multipart_upload(data, key)
        if not self.verify(manifest):
            raise ValueError("S3 verification failed")
        self.client.put_object(Bucket=self.bucket, Key=key + ".json", Body=manifest.encode(), IfNoneMatch="*")

    def _multipart_upload(self, data: BinaryIO, key: str) -> None:
        upload = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)['UploadId']
        parts = []
        try:
            while chunk := data.read(MULTIPART_PART_SIZE):
                number = len(parts) + 1
                if number > 10000:
                    raise ValueError('backup is too large for multipart upload')
                response = self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload,
                                                   PartNumber=number, Body=chunk)
                parts.append({'PartNumber': number, 'ETag': response['ETag']})
            self.client.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload,
                                                  MultipartUpload={'Parts': parts}, IfNoneMatch='*')
        except Exception:
            try:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload)
            except Exception:
                pass
            raise

    def verify(self, manifest: Manifest) -> bool:
        stream = self.client.get_object(Bucket=self.bucket, Key=self.key(manifest))["Body"]
        try:
            return digest(stream) == (manifest.sha256, manifest.size)
        finally:
            stream.close()


def copy_verified(stream: BinaryIO, target: BinaryIO, manifest: Manifest) -> None:
    """Copy a stream and reject incomplete or altered data before use."""
    checksum = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        target.write(chunk)
        checksum.update(chunk)
        size += len(chunk)
    if (checksum.hexdigest(), size) != (manifest.sha256, manifest.size):
        raise ValueError("backup checksum or size mismatch")


def local_get(store: LocalStorage, manifest: Manifest, target: BinaryIO) -> None:
    with (store.directory(manifest) / manifest.artifact).open("rb") as stream:
        copy_verified(stream, target, manifest)


def s3_list(store: S3Storage, project: str, source: str) -> list[Manifest]:
    project, source = validate_project(project), valid_name(source)
    prefix = "/".join(p for p in (store.prefix, project, source, "") if p) + "/"
    found = []
    for page in store.client.get_paginator("list_objects_v2").paginate(Bucket=store.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.endswith(".json") or "/" in key[len(prefix):]:
                continue
            body = store.client.get_object(Bucket=store.bucket, Key=key)["Body"]
            try:
                manifest = Manifest.decode(body.read(65537))
            except (ValueError, TypeError, UnicodeError):
                continue
            finally:
                body.close()
            if manifest.project == project and manifest.source == source and store.key(manifest) + ".json" == key:
                from botocore.exceptions import ClientError
                try:
                    info = store.client.head_object(Bucket=store.bucket, Key=store.key(manifest))
                except ClientError as exc:
                    if exc.response.get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                        raise
                    print(f'incomplete backup: {manifest.artifact} (data missing)', file=sys.stderr)
                    continue
                if info.get('ContentLength') != manifest.size:
                    print(f'incomplete backup: {manifest.artifact} (size mismatch)', file=sys.stderr)
                    continue
                found.append(manifest)
    return sorted(found, key=lambda m: datetime.fromisoformat(m.timestamp.replace('Z', '+00:00')))


def s3_get(store: S3Storage, manifest: Manifest, target: BinaryIO) -> None:
    stream = store.client.get_object(Bucket=store.bucket, Key=store.key(manifest))["Body"]
    try:
        copy_verified(stream, target, manifest)
    finally:
        stream.close()


def prune_local(store: LocalStorage, project: str, source: str, days: int, now) -> int:
    """Remove indexed expired local copies without following directory symlinks."""
    from datetime import datetime, timedelta, timezone
    if days < 1:
        raise ValueError('local retention days must be positive')
    cutoff = now.astimezone(timezone.utc) - timedelta(days=days)
    folder = (store.root / validate_project(project) / valid_name(source)).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in folder.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        removed = 0
        for name in os.listdir(fd):
            if not name.endswith('.json'):
                continue
            try:
                valid_name(name[:-5])
                meta_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(meta_fd, 'rb') as metadata:
                    if not stat.S_ISREG(os.fstat(metadata.fileno()).st_mode):
                        continue
                    manifest = Manifest.decode(metadata.read(65537))
                if (manifest.project, manifest.source, manifest.artifact + '.json') != (project, source, name):
                    continue
                created = datetime.fromisoformat(manifest.timestamp.replace('Z', '+00:00'))
                if created.tzinfo is None or created >= cutoff:
                    continue
                artifact_stat = os.stat(manifest.artifact, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(artifact_stat.st_mode):
                    raise ValueError('refusing to remove non-regular backup')
                os.unlink(name, dir_fd=fd)
                os.unlink(manifest.artifact, dir_fd=fd)
                removed += 1
            except (FileNotFoundError, ValueError, TypeError):
                continue
        return removed
    finally:
        os.close(fd)


def write_private_copy(source: BinaryIO, destination: Path) -> None:
    """Publish verified bytes privately through a pinned destination directory."""
    parent = destination.parent
    if destination.name in ('', '.', '..'):
        raise ValueError('invalid destination name')
    before = os.lstat(parent)
    dirfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temp = '.cli-backup-download-' + secrets.token_hex(16)
    created = False
    published = False
    try:
        pinned = os.fstat(dirfd)
        if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise OSError('destination directory changed')
        writable_by_others = pinned.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if pinned.st_uid not in (os.geteuid(), 0) or (writable_by_others and not pinned.st_mode & stat.S_ISVTX):
            raise OSError('destination directory is not protected from other users')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temp, flags, 0o600, dir_fd=dirfd)
        created = True
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            info = os.fstat(target.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError('invalid temporary file')
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
            os.link(temp, destination.name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
            published = True
            linked = os.stat(destination.name, dir_fd=dirfd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                raise OSError('temporary file changed during publication')
        current = os.lstat(parent)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise OSError('destination directory changed')
        os.fsync(dirfd)
    except Exception:
        if published:
            try:
                os.unlink(destination.name, dir_fd=dirfd)
                os.fsync(dirfd)
            except OSError:
                pass
        raise
    finally:
        try:
            if created:
                os.unlink(temp, dir_fd=dirfd)
        finally:
            os.close(dirfd)
