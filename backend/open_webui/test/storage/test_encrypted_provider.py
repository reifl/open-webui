import asyncio
import io
import os
import re
from pathlib import Path

import pytest

from open_webui.storage.encryption import MAGIC, FileCipher, FileEncryptionError
from open_webui.storage.provider import EncryptedStorageProvider, StorageProvider

BACKEND_DIR = Path(__file__).resolve().parents[2]


class FakeProvider(StorageProvider):
    """Stores files in a directory and records exactly what it was given."""

    def __init__(self, root: Path):
        self.root = root
        self.uploaded = {}

    def upload_file(self, file, filename, tags):
        contents = file.read()
        self.uploaded[filename] = contents
        path = self.root / filename
        path.write_bytes(contents)
        return contents, str(path)

    def get_file(self, file_path):
        return file_path

    def delete_file(self, file_path):
        os.remove(file_path)

    def delete_all_files(self):
        for p in self.root.iterdir():
            p.unlink()


def make_storage(tmp_path, encrypt=True, cipher=True):
    root = tmp_path / 'uploads'
    root.mkdir()
    inner = FakeProvider(root)
    c = FileCipher({'k1': os.urandom(32)}, 'k1') if cipher else None
    return EncryptedStorageProvider(inner, c, encrypt_writes=encrypt, temp_dir=str(tmp_path / 'tmp')), inner


def test_upload_encrypts_and_returns_plaintext(tmp_path):
    storage, inner = make_storage(tmp_path)
    data = b'\x89PNG fake image data' * 1000
    contents, path = storage.upload_file(io.BytesIO(data), 'a.png', {})

    assert contents == data
    assert inner.uploaded['a.png'].startswith(MAGIC)
    assert Path(path).read_bytes().startswith(MAGIC)
    assert data not in Path(path).read_bytes()
    assert storage.read_bytes(path) == data
    assert storage.read_bytes(path, limit=10) == data[:10]


def test_empty_upload_rejected(tmp_path):
    storage, _ = make_storage(tmp_path)
    with pytest.raises(ValueError):
        storage.upload_file(io.BytesIO(b''), 'empty.txt', {})


def test_disabled_writes_plaintext_but_reads_encrypted(tmp_path):
    storage, inner = make_storage(tmp_path)
    _, enc_path = storage.upload_file(io.BytesIO(b'encrypted'), 'enc.txt', {})

    storage.encrypt_writes = False
    _, plain_path = storage.upload_file(io.BytesIO(b'plain'), 'plain.txt', {})

    assert Path(plain_path).read_bytes() == b'plain'
    assert storage.read_bytes(enc_path) == b'encrypted'
    assert storage.read_bytes(plain_path) == b'plain'


def test_encrypted_file_without_keys_fails(tmp_path):
    storage, _ = make_storage(tmp_path)
    _, path = storage.upload_file(io.BytesIO(b'secret'), 's.txt', {})
    storage.cipher = None
    with pytest.raises(FileEncryptionError):
        storage.read_bytes(path)


def test_enabled_without_key_fails_fast(tmp_path):
    with pytest.raises(RuntimeError):
        make_storage(tmp_path, encrypt=True, cipher=False)


def test_open_range_and_iter_range(tmp_path):
    storage, _ = make_storage(tmp_path)
    data = os.urandom(200_000)
    _, path = storage.upload_file(io.BytesIO(data), 'v.mp4', {})

    size, encrypted, local_path = storage.open_range(path)
    assert (size, encrypted) == (len(data), True)
    assert b''.join(storage.iter_range(local_path, 1000, 70_000)) == data[1000:70_001]

    storage.encrypt_writes = False
    _, plain = storage.upload_file(io.BytesIO(b'abc'), 'p.txt', {})
    assert storage.open_range(plain)[:2] == (3, False)


def test_local_plaintext_path_cleans_up(tmp_path):
    storage, _ = make_storage(tmp_path)
    _, path = storage.upload_file(io.BytesIO(b'audio bytes'), 'rec.wav', {})

    with storage.local_plaintext_path(path) as plain_path:
        assert plain_path != path
        assert plain_path.endswith('rec.wav')
        assert Path(plain_path).read_bytes() == b'audio bytes'
        # Consumers such as transcribe() write derived files next to the input.
        Path(plain_path).with_suffix('.mp3').write_bytes(b'converted')

    assert os.listdir(tmp_path / 'tmp') == []


def test_local_plaintext_path_cleans_up_on_error(tmp_path):
    storage, _ = make_storage(tmp_path)
    _, path = storage.upload_file(io.BytesIO(b'doc'), 'd.pdf', {})

    async def run():
        async with storage.alocal_plaintext_path(path) as plain_path:
            assert Path(plain_path).read_bytes() == b'doc'
            raise RuntimeError('loader failed')

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert os.listdir(tmp_path / 'tmp') == []


def test_local_plaintext_path_passthrough_for_plaintext(tmp_path):
    storage, _ = make_storage(tmp_path, encrypt=False)
    _, path = storage.upload_file(io.BytesIO(b'plain'), 'x.txt', {})
    with storage.local_plaintext_path(path) as plain_path:
        assert plain_path == path


def test_no_direct_storage_get_file_callers():
    """Stored files must be read via read_bytes/open_range/local_plaintext_path so they get decrypted."""
    offenders = []
    for path in BACKEND_DIR.rglob('*.py'):
        if 'test' in path.parts or path.name == 'provider.py':
            continue
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if re.search(r'\bStorage\.get_file\b', line):
                offenders.append(f'{path.relative_to(BACKEND_DIR)}:{lineno}')
    assert offenders == []


def test_stale_temp_dirs_are_swept_on_startup(tmp_path):
    temp_dir = tmp_path / 'tmp'
    stale, fresh = temp_dir / 'stale', temp_dir / 'fresh'
    stale.mkdir(parents=True)
    fresh.mkdir()
    (stale / 'leftover.pdf').write_bytes(b'plaintext')
    os.utime(stale, (0, 0))

    EncryptedStorageProvider(FakeProvider(tmp_path), None, encrypt_writes=False, temp_dir=str(temp_dir))

    assert not stale.exists()
    assert fresh.exists()
