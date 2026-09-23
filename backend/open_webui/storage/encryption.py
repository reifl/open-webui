"""
Encryption at rest for stored files.

File format (all integers big-endian):

    MAGIC            6 bytes   b'OWENC\\x01'
    kid_len          1 byte
    kid              kid_len bytes (utf-8)
    wrapped_dek_len  2 bytes
    wrapped_dek      nonce(12) || AES-256-GCM(kek, dek) (48)
    base_nonce       8 bytes
    chunk_size       4 bytes
    plaintext_size   8 bytes
    body             N x (AES-256-GCM(dek, chunk_i) || tag(16))

Every file gets its own random data encryption key (DEK), wrapped with the
configured key encryption key (KEK). Chunk i uses nonce base_nonce || i and
authenticates sha256(header) || is_last, which detects tampering, reordering
and truncation. Fixed-size chunks allow decrypting arbitrary byte ranges.
"""

import base64
import hashlib
import io
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Dict, Iterator, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'OWENC\x01'
DEFAULT_CHUNK_SIZE = 64 * 1024
TAG_SIZE = 16
KEY_SIZE = 32
NONCE_SIZE = 12
BASE_NONCE_SIZE = 8


class FileEncryptionError(Exception):
    pass


def is_encrypted(header: bytes) -> bool:
    return header[: len(MAGIC)] == MAGIC


def read_key_id(f: BinaryIO) -> Optional[str]:
    """Return the key id of an encrypted file, or None for plaintext files."""
    f.seek(0)
    fixed = f.read(len(MAGIC) + 1)
    if not is_encrypted(fixed) or len(fixed) != len(MAGIC) + 1:
        return None
    kid = f.read(fixed[-1]).decode('utf-8')
    f.seek(0)
    return kid


def parse_keys(value: str) -> Dict[str, bytes]:
    """Parse 'kid1:<base64>,kid2:<base64>' into {kid: 32-byte key}."""
    keys = {}
    for entry in (value or '').split(','):
        entry = entry.strip()
        if not entry:
            continue
        kid, sep, encoded = entry.partition(':')
        kid = kid.strip()
        if not sep or not kid:
            raise FileEncryptionError("Invalid FILE_ENCRYPTION_KEYS entry, expected 'kid:base64key'")
        if len(kid.encode('utf-8')) > 255:
            raise FileEncryptionError(f'Key id too long: {kid[:32]}...')
        try:
            key = base64.b64decode(encoded.strip(), validate=True)
        except Exception:
            try:
                key = base64.urlsafe_b64decode(encoded.strip())
            except Exception as e:
                raise FileEncryptionError(f"Key '{kid}' is not valid base64") from e
        if len(key) != KEY_SIZE:
            raise FileEncryptionError(f"Key '{kid}' must decode to {KEY_SIZE} bytes, got {len(key)}")
        keys[kid] = key
    return keys


@dataclass
class _Header:
    kid: str
    dek: bytes
    base_nonce: bytes
    chunk_size: int
    plaintext_size: int
    header_len: int
    header_hash: bytes

    @property
    def chunk_count(self) -> int:
        # An empty file still has one (empty) final chunk.
        return max(1, -(-self.plaintext_size // self.chunk_size))


class FileCipher:
    def __init__(self, keys: Dict[str, bytes], active_kid: Optional[str] = None):
        if active_kid is not None and active_kid not in keys:
            raise FileEncryptionError(f"Active key id '{active_kid}' is not present in FILE_ENCRYPTION_KEYS")
        self.keys = keys
        self.active_kid = active_kid

    # --- writing ---

    def encrypt_bytes(self, data: bytes, chunk_size: int = DEFAULT_CHUNK_SIZE) -> bytes:
        out = io.BytesIO()
        self.encrypt_stream(io.BytesIO(data), out, len(data), chunk_size)
        return out.getvalue()

    def encrypt_stream(self, src: BinaryIO, dst: BinaryIO, size: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        """Encrypt exactly `size` bytes from src into dst, one chunk at a time."""
        if not self.active_kid:
            raise FileEncryptionError('No active file encryption key configured')

        kid = self.active_kid.encode('utf-8')
        dek = AESGCM.generate_key(bit_length=256)
        wrap_nonce = os.urandom(NONCE_SIZE)
        wrapped = wrap_nonce + AESGCM(self.keys[self.active_kid]).encrypt(wrap_nonce, dek, MAGIC + kid)
        base_nonce = os.urandom(BASE_NONCE_SIZE)

        header = (
            MAGIC
            + struct.pack('>B', len(kid))
            + kid
            + struct.pack('>H', len(wrapped))
            + wrapped
            + base_nonce
            + struct.pack('>IQ', chunk_size, size)
        )
        header_hash = hashlib.sha256(header).digest()
        dst.write(header)

        aesgcm = AESGCM(dek)
        count = max(1, -(-size // chunk_size))
        for i in range(count):
            expected = min(chunk_size, size - i * chunk_size)
            chunk = src.read(expected)
            if len(chunk) != expected:
                raise FileEncryptionError('Source changed size while encrypting')
            dst.write(aesgcm.encrypt(_nonce(base_nonce, i), chunk, _aad(header_hash, i == count - 1)))

    # --- reading ---

    def _read_header(self, f: BinaryIO) -> _Header:
        f.seek(0)
        fixed = f.read(len(MAGIC) + 1)
        if not is_encrypted(fixed) or len(fixed) != len(MAGIC) + 1:
            raise FileEncryptionError('Not an encrypted file')
        kid_raw = f.read(fixed[-1])
        wrapped_len_raw = f.read(2)
        if len(kid_raw) != fixed[-1] or len(wrapped_len_raw) != 2:
            raise FileEncryptionError('Truncated encryption header')
        (wrapped_len,) = struct.unpack('>H', wrapped_len_raw)
        wrapped = f.read(wrapped_len)
        base_nonce = f.read(BASE_NONCE_SIZE)
        sizes = f.read(12)
        if len(wrapped) != wrapped_len or len(base_nonce) != BASE_NONCE_SIZE or len(sizes) != 12:
            raise FileEncryptionError('Truncated encryption header')
        chunk_size, plaintext_size = struct.unpack('>IQ', sizes)
        if chunk_size <= 0:
            raise FileEncryptionError('Invalid chunk size in encryption header')

        kid = kid_raw.decode('utf-8')
        kek = self.keys.get(kid)
        if kek is None:
            raise FileEncryptionError(f"File was encrypted with unknown key id '{kid}'")
        try:
            dek = AESGCM(kek).decrypt(wrapped[:NONCE_SIZE], wrapped[NONCE_SIZE:], MAGIC + kid_raw)
        except InvalidTag as e:
            raise FileEncryptionError(f"Could not unwrap file key with key id '{kid}'") from e

        header = fixed + kid_raw + wrapped_len_raw + wrapped + base_nonce + sizes
        return _Header(
            kid=kid,
            dek=dek,
            base_nonce=base_nonce,
            chunk_size=chunk_size,
            plaintext_size=plaintext_size,
            header_len=len(header),
            header_hash=hashlib.sha256(header).digest(),
        )

    def plaintext_size(self, f: BinaryIO) -> int:
        return self._read_header(f).plaintext_size

    def decrypt_range(self, f: BinaryIO, start: int = 0, end: Optional[int] = None) -> Iterator[bytes]:
        """Yield plaintext bytes [start, end] (end inclusive, None = until EOF)."""
        h = self._read_header(f)
        if h.plaintext_size == 0 or (end is not None and end < start):
            # Still authenticate the empty final chunk so tampering is detected.
            if h.plaintext_size == 0:
                self._decrypt_chunk(f, h, 0)
            return
        last_byte = h.plaintext_size - 1 if end is None else min(end, h.plaintext_size - 1)
        if start > last_byte:
            return

        first_chunk = start // h.chunk_size
        last_chunk = last_byte // h.chunk_size
        for i in range(first_chunk, last_chunk + 1):
            plain = self._decrypt_chunk(f, h, i)
            chunk_start = i * h.chunk_size
            lo = max(start - chunk_start, 0)
            hi = min(last_byte - chunk_start + 1, len(plain))
            yield plain[lo:hi]

    def decrypt_all(self, f: BinaryIO) -> bytes:
        return b''.join(self.decrypt_range(f))

    def _decrypt_chunk(self, f: BinaryIO, h: _Header, i: int) -> bytes:
        is_last = i == h.chunk_count - 1
        expected_plain = h.plaintext_size - i * h.chunk_size if is_last else h.chunk_size
        f.seek(h.header_len + i * (h.chunk_size + TAG_SIZE))
        data = f.read(expected_plain + TAG_SIZE)
        if len(data) != expected_plain + TAG_SIZE:
            raise FileEncryptionError(f'Encrypted file is truncated (chunk {i})')
        try:
            return AESGCM(h.dek).decrypt(_nonce(h.base_nonce, i), data, _aad(h.header_hash, is_last))
        except InvalidTag as e:
            raise FileEncryptionError(f'Encrypted file failed integrity check (chunk {i})') from e


def _nonce(base_nonce: bytes, index: int) -> bytes:
    return base_nonce + struct.pack('>I', index)


def _aad(header_hash: bytes, is_last: bool) -> bytes:
    return header_hash + (b'\x01' if is_last else b'\x00')
