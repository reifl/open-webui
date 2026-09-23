"""
Encrypt existing stored files in place.

Run inside the backend (same environment variables as the server):

    python -m open_webui.storage.migrate_encryption --dry-run
    python -m open_webui.storage.migrate_encryption
    python -m open_webui.storage.migrate_encryption --rotate            # re-encrypt files not using the active key
    python -m open_webui.storage.migrate_encryption --include-orphans   # also files in uploads/ without a DB entry

Safe to run while the server is up: every file is encrypted into a temp file next to
the original, decrypted again and compared by SHA-256, and only then atomically
swapped in. Files that are already encrypted with the active key are skipped, so the
command can be re-run after an interruption.
"""

import argparse
import asyncio
import hashlib
import io
import logging
import os
import shutil
import sys
import tempfile
import time
from typing import BinaryIO, Optional

from sqlalchemy import select

from open_webui.config import ENABLE_FILE_ENCRYPTION, STORAGE_LOCAL_CACHE, STORAGE_PROVIDER, UPLOAD_DIR
from open_webui.internal.db import get_async_db_context
from open_webui.models.files import File
from open_webui.storage.encryption import read_key_id
from open_webui.storage.provider import Storage

log = logging.getLogger(__name__)

TMP_PREFIX = '.owenc-migrate-'
HASH_BLOCK = 1024 * 1024


class _HashingReader(io.RawIOBase):
    """Read-through wrapper that hashes everything read from the underlying file."""

    def __init__(self, f: BinaryIO):
        self.f = f
        self.sha256 = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        data = self.f.read(size)
        self.sha256.update(data)
        return data


def _sha256_of_decrypted(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in Storage.cipher.decrypt_range(f):
            sha.update(chunk)
    return sha.hexdigest()


def _encrypt_to_temp(plain_path: str, target_dir: str) -> tuple[str, str]:
    """Encrypt plain_path into a temp file in target_dir. Returns (temp_path, plaintext_sha256)."""
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=TMP_PREFIX, suffix='.tmp')
    try:
        with open(plain_path, 'rb') as src, os.fdopen(fd, 'wb') as dst:
            size = os.fstat(src.fileno()).st_size
            reader = _HashingReader(src)
            Storage.cipher.encrypt_stream(reader, dst, size)
            dst.flush()
            os.fsync(dst.fileno())
        return tmp_path, reader.sha256.hexdigest()
    except BaseException:
        _remove(tmp_path)
        raise


def _remove(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _replace_preserving_metadata(tmp_path: str, local_path: str) -> None:
    st = os.stat(local_path)
    shutil.copystat(local_path, tmp_path)
    if hasattr(os, 'chown'):
        try:
            os.chown(tmp_path, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    os.replace(tmp_path, local_path)


def migrate_file(stored_path: str, rotate: bool, dry_run: bool) -> str:
    """Encrypt one stored file. Returns a status: encrypted, rotated, skipped, missing."""
    local_path = Storage.inner.get_file(stored_path)
    if not os.path.isfile(local_path):
        return 'missing'

    with open(local_path, 'rb') as f:
        kid = read_key_id(f)

    if kid is not None and (not rotate or kid == Storage.cipher.active_kid):
        return 'skipped'
    status = 'rotated' if kid is not None else 'encrypted'
    if dry_run:
        return status

    plain_path, plain_tmp_dir, enc_tmp = local_path, None, None
    try:
        if kid is not None:
            # Rotation: decrypt into the private temp dir first, then encrypt with the active key.
            os.makedirs(Storage.temp_dir, mode=0o700, exist_ok=True)
            plain_tmp_dir = tempfile.mkdtemp(dir=Storage.temp_dir)
            plain_path = os.path.join(plain_tmp_dir, 'plain')
            with open(plain_path, 'wb') as out, open(local_path, 'rb') as f:
                for chunk in Storage.cipher.decrypt_range(f):
                    out.write(chunk)

        enc_tmp, plain_sha = _encrypt_to_temp(plain_path, os.path.dirname(local_path))
        if _sha256_of_decrypted(enc_tmp) != plain_sha:
            raise RuntimeError('verification failed: decrypted content does not match the original')

        if STORAGE_PROVIDER == 'local':
            _replace_preserving_metadata(enc_tmp, local_path)
            enc_tmp = None
        else:
            with open(enc_tmp, 'rb') as f:
                _, new_path = Storage.inner.upload_file(f, os.path.basename(local_path), {})
            if new_path != stored_path:
                raise RuntimeError(
                    f'cloud object was written to {new_path}, expected {stored_path}; '
                    'the original object is unchanged, please check the bucket'
                )
            if not STORAGE_LOCAL_CACHE:
                _remove(local_path)
        return status
    finally:
        _remove(enc_tmp)
        if plain_tmp_dir:
            shutil.rmtree(plain_tmp_dir, ignore_errors=True)


def cleanup_stale_temp_files() -> int:
    removed = 0
    for entry in os.scandir(UPLOAD_DIR):
        if entry.name.startswith(TMP_PREFIX) and entry.is_file(follow_symlinks=False):
            _remove(entry.path)
            removed += 1
    return removed


async def load_stored_paths() -> list[tuple[str, str]]:
    async with get_async_db_context() as db:
        result = await db.execute(select(File.id, File.path).where(File.path.isnot(None)))
        return [(row.id, row.path) for row in result if row.path]


def find_orphans(stored_paths: list[str]) -> list[str]:
    referenced = {os.path.basename(p) for p in stored_paths}
    return sorted(
        entry.path
        for entry in os.scandir(UPLOAD_DIR)
        if entry.is_file(follow_symlinks=False)
        and entry.name not in referenced
        and not entry.name.startswith(TMP_PREFIX)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description='Encrypt existing stored files in place.')
    parser.add_argument('--dry-run', action='store_true', help='only report what would change')
    parser.add_argument('--rotate', action='store_true', help='re-encrypt files that use a non-active key')
    parser.add_argument(
        '--include-orphans',
        action='store_true',
        help='also encrypt files in the upload dir that no DB entry references (local storage only)',
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if Storage.cipher is None or not Storage.cipher.active_kid:
        print('FILE_ENCRYPTION_KEYS / FILE_ENCRYPTION_ACTIVE_KEY_ID are not configured.', file=sys.stderr)
        return 2
    if not ENABLE_FILE_ENCRYPTION:
        print('Warning: ENABLE_FILE_ENCRYPTION is off, new uploads will still be stored in plaintext.')
    if args.include_orphans and STORAGE_PROVIDER != 'local':
        print('--include-orphans is only supported for STORAGE_PROVIDER=local.', file=sys.stderr)
        return 2

    if not args.dry_run:
        stale = cleanup_stale_temp_files()
        if stale:
            print(f'Removed {stale} temp file(s) from an interrupted run.')

    rows = asyncio.run(load_stored_paths())
    paths = [path for _, path in rows]
    targets = list(rows)

    if STORAGE_PROVIDER == 'local':
        orphans = find_orphans(paths)
        if args.include_orphans:
            targets += [('(orphan)', p) for p in orphans]
        elif orphans:
            print(
                f'Note: {len(orphans)} file(s) in {UPLOAD_DIR} have no DB entry and are left untouched '
                '(use --include-orphans to encrypt them too).'
            )

    mode = 'DRY RUN, nothing is changed' if args.dry_run else 'encrypting'
    print(f'{len(targets)} file(s) to check, active key "{Storage.cipher.active_kid}" ({mode}).')

    counts = {'encrypted': 0, 'rotated': 0, 'skipped': 0, 'missing': 0, 'failed': 0}
    started = time.monotonic()
    for i, (file_id, path) in enumerate(targets, 1):
        try:
            counts[migrate_file(path, args.rotate, args.dry_run)] += 1
        except Exception as e:
            counts['failed'] += 1
            print(f'  FAILED {file_id} {path}: {e}', file=sys.stderr)
        if i % 100 == 0 or i == len(targets):
            print(f'  {i}/{len(targets)} checked ({time.monotonic() - started:.0f}s)')

    verb = 'would be ' if args.dry_run else ''
    print(
        f'Done: {counts["encrypted"]} {verb}encrypted, {counts["rotated"]} {verb}re-encrypted with the active key, '
        f'{counts["skipped"]} already encrypted, {counts["missing"]} missing on disk, {counts["failed"]} failed.'
    )
    return 1 if counts['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
