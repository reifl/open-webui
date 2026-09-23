import base64
import io
import os

import pytest

from open_webui.storage.encryption import (
    DEFAULT_CHUNK_SIZE,
    MAGIC,
    FileCipher,
    FileEncryptionError,
    is_encrypted,
    parse_keys,
)

KEY_A = os.urandom(32)
KEY_B = os.urandom(32)


def cipher(active='a'):
    return FileCipher({'a': KEY_A, 'b': KEY_B}, active)


@pytest.mark.parametrize('size', [0, 1, DEFAULT_CHUNK_SIZE - 1, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_SIZE + 1, 3_000_000])
def test_round_trip(size):
    data = os.urandom(size)
    blob = cipher().encrypt_bytes(data)
    assert is_encrypted(blob)
    assert data not in blob or size == 0
    assert cipher().decrypt_all(io.BytesIO(blob)) == data
    assert cipher().plaintext_size(io.BytesIO(blob)) == size


def test_ranges_across_chunk_boundaries():
    data = os.urandom(DEFAULT_CHUNK_SIZE * 3 + 123)
    blob = cipher().encrypt_bytes(data)
    c = cipher()
    for start, end in [
        (0, 0),
        (0, 99),
        (DEFAULT_CHUNK_SIZE - 10, DEFAULT_CHUNK_SIZE + 10),
        (DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_SIZE * 2 - 1),
        (len(data) - 5, None),
        (len(data) - 5, len(data) + 1000),
        (100, DEFAULT_CHUNK_SIZE * 3),
    ]:
        expected = data[start : (len(data) if end is None else end + 1)]
        assert b''.join(c.decrypt_range(io.BytesIO(blob), start, end)) == expected
    assert b''.join(c.decrypt_range(io.BytesIO(blob), len(data), None)) == b''


def test_each_file_uses_fresh_key_and_nonce():
    data = b'same content'
    assert cipher().encrypt_bytes(data) != cipher().encrypt_bytes(data)


def _flip(blob: bytes, pos: int) -> bytes:
    b = bytearray(blob)
    b[pos] ^= 0x01
    return bytes(b)


def test_tampered_body_is_rejected():
    blob = cipher().encrypt_bytes(os.urandom(DEFAULT_CHUNK_SIZE * 2))
    with pytest.raises(FileEncryptionError):
        cipher().decrypt_all(io.BytesIO(_flip(blob, len(blob) - 20)))


def test_tampered_header_is_rejected():
    blob = cipher().encrypt_bytes(b'hello world')
    # Flip a byte in the plaintext size field (last 8 bytes of the header).
    header_len = len(blob) - len(b'hello world') - 16
    with pytest.raises(FileEncryptionError):
        cipher().decrypt_all(io.BytesIO(_flip(blob, header_len - 1)))


@pytest.mark.parametrize('cut', [1, 16, DEFAULT_CHUNK_SIZE + 16])
def test_truncation_is_rejected(cut):
    blob = cipher().encrypt_bytes(os.urandom(DEFAULT_CHUNK_SIZE * 2 + 50))
    with pytest.raises(FileEncryptionError):
        cipher().decrypt_all(io.BytesIO(blob[:-cut]))


def test_swapped_chunks_are_rejected():
    data = os.urandom(DEFAULT_CHUNK_SIZE * 3)
    blob = cipher().encrypt_bytes(data)
    body_start = len(blob) - 3 * (DEFAULT_CHUNK_SIZE + 16)
    size = DEFAULT_CHUNK_SIZE + 16
    c0 = blob[body_start : body_start + size]
    c1 = blob[body_start + size : body_start + 2 * size]
    swapped = blob[:body_start] + c1 + c0 + blob[body_start + 2 * size :]
    with pytest.raises(FileEncryptionError):
        cipher().decrypt_all(io.BytesIO(swapped))


def test_key_rotation_reads_old_files():
    old_blob = cipher('a').encrypt_bytes(b'old')
    new_cipher = cipher('b')
    assert new_cipher.decrypt_all(io.BytesIO(old_blob)) == b'old'
    assert b'\x01b' in new_cipher.encrypt_bytes(b'new')[: len(MAGIC) + 2]


def test_unknown_key_id_fails_clearly():
    blob = cipher('a').encrypt_bytes(b'secret')
    with pytest.raises(FileEncryptionError, match="unknown key id 'a'"):
        FileCipher({'b': KEY_B}, 'b').decrypt_all(io.BytesIO(blob))


def test_wrong_key_material_fails():
    blob = cipher('a').encrypt_bytes(b'secret')
    with pytest.raises(FileEncryptionError):
        FileCipher({'a': KEY_B}, 'a').decrypt_all(io.BytesIO(blob))


def test_plaintext_is_not_detected_as_encrypted():
    assert not is_encrypted(b'%PDF-1.7 ...')
    assert not is_encrypted(b'')


def test_parse_keys():
    value = f'a:{base64.b64encode(KEY_A).decode()}, b:{base64.urlsafe_b64encode(KEY_B).decode()}'
    assert parse_keys(value) == {'a': KEY_A, 'b': KEY_B}
    assert parse_keys('') == {}
    with pytest.raises(FileEncryptionError):
        parse_keys('nokey')
    with pytest.raises(FileEncryptionError):
        parse_keys(f'a:{base64.b64encode(os.urandom(16)).decode()}')


def test_active_key_must_exist():
    with pytest.raises(FileEncryptionError):
        FileCipher({'a': KEY_A}, 'missing')
