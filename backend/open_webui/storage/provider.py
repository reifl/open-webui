import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, BinaryIO, Dict, Iterator, Optional, Tuple

from open_webui.config import (
    AZURE_STORAGE_CONTAINER_NAME,
    AZURE_STORAGE_ENDPOINT,
    AZURE_STORAGE_KEY,
    ENABLE_FILE_ENCRYPTION,
    FILE_ENCRYPTION_ACTIVE_KEY_ID,
    FILE_ENCRYPTION_KEYS,
    FILE_ENCRYPTION_TEMP_DIR,
    GCS_BUCKET_NAME,
    GOOGLE_APPLICATION_CREDENTIALS_JSON,
    S3_ACCESS_KEY_ID,
    S3_ADDRESSING_STYLE,
    S3_BUCKET_NAME,
    S3_ENABLE_TAGGING,
    S3_ENDPOINT_URL,
    S3_KEY_PREFIX,
    S3_REGION_NAME,
    S3_SECRET_ACCESS_KEY,
    S3_USE_ACCELERATE_ENDPOINT,
    STORAGE_PROVIDER,
    UPLOAD_DIR,
)
from open_webui.constants import ERROR_MESSAGES
from open_webui.storage.encryption import MAGIC, FileCipher, FileEncryptionError, is_encrypted, parse_keys
from open_webui.utils.json_codec import JSONCodec

from open_webui.env import USE_SLIM

if not USE_SLIM:
    import boto3
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from google.cloud import storage
    from google.cloud.exceptions import GoogleCloudError, NotFound

log = logging.getLogger(__name__)


class StorageProvider(ABC):
    @abstractmethod
    def get_file(self, file_path: str) -> str:
        pass

    @abstractmethod
    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        pass

    @abstractmethod
    def delete_all_files(self) -> None:
        pass

    @abstractmethod
    def delete_file(self, file_path: str) -> None:
        pass


class LocalStorageProvider(StorageProvider):
    @staticmethod
    def upload_file(file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
        file_path = os.path.join(UPLOAD_DIR, filename)
        with open(file_path, 'wb') as f:
            f.write(contents)
        return contents, file_path

    @staticmethod
    def get_file(file_path: str) -> str:
        """Handles downloading of the file from local storage."""
        return file_path

    @staticmethod
    def delete_file(file_path: str) -> None:
        """Handles deletion of the file from local storage."""
        filename = os.path.basename(file_path)
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.isfile(file_path):
            os.remove(file_path)
        else:
            log.warning(f'File {file_path} not found in local storage.')

    @staticmethod
    def delete_all_files() -> None:
        """Handles deletion of all files from local storage."""
        if os.path.exists(UPLOAD_DIR):
            for filename in os.listdir(UPLOAD_DIR):
                file_path = os.path.join(UPLOAD_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)  # Remove the file or link
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)  # Remove the directory
                except Exception as e:
                    log.exception(f'Failed to delete {file_path}. Reason: {e}')
        else:
            log.warning(f'Directory {UPLOAD_DIR} not found in local storage.')


class S3StorageProvider(StorageProvider):
    def __init__(self):
        config = Config(
            s3={
                'use_accelerate_endpoint': S3_USE_ACCELERATE_ENDPOINT,
                'addressing_style': S3_ADDRESSING_STYLE,
            },
            # KIT change - see https://github.com/boto/boto3/issues/4400#issuecomment-2600742103∆
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        )

        # If access key and secret are provided, use them for authentication
        if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
            self.s3_client = boto3.client(
                's3',
                region_name=S3_REGION_NAME,
                endpoint_url=S3_ENDPOINT_URL,
                aws_access_key_id=S3_ACCESS_KEY_ID,
                aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                config=config,
            )
        else:
            # If no explicit credentials are provided, fall back to default AWS credentials
            # This supports workload identity (IAM roles for EC2, EKS, etc.)
            self.s3_client = boto3.client(
                's3',
                region_name=S3_REGION_NAME,
                endpoint_url=S3_ENDPOINT_URL,
                config=config,
            )

        self.bucket_name = S3_BUCKET_NAME
        self.key_prefix = S3_KEY_PREFIX if S3_KEY_PREFIX else ''

    @staticmethod
    def sanitize_tag_value(s: str) -> str:
        """Only include S3 allowed characters."""
        return re.sub(r'[^a-zA-Z0-9 äöüÄÖÜß\+\-=\._:/@]', '', s)

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        """Handles uploading of the file to S3 storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        s3_key = os.path.join(self.key_prefix, filename)
        try:
            self.s3_client.upload_file(file_path, self.bucket_name, s3_key)
            if S3_ENABLE_TAGGING and tags:
                sanitized_tags = {self.sanitize_tag_value(k): self.sanitize_tag_value(v) for k, v in tags.items()}
                tagging = {'TagSet': [{'Key': k, 'Value': v} for k, v in sanitized_tags.items()]}
                self.s3_client.put_object_tagging(
                    Bucket=self.bucket_name,
                    Key=s3_key,
                    Tagging=tagging,
                )
            return (
                contents,
                f's3://{self.bucket_name}/{s3_key}',
            )
        except ClientError as e:
            raise RuntimeError(f'Error uploading file to S3: {e}')

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from S3 storage."""
        try:
            s3_key = self._extract_s3_key(file_path)
            local_file_path = self._get_local_file_path(s3_key)
            self.s3_client.download_file(self.bucket_name, s3_key, local_file_path)
            return local_file_path
        except ClientError as e:
            raise RuntimeError(f'Error downloading file from S3: {e}')

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from S3 storage."""
        try:
            s3_key = self._extract_s3_key(file_path)
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
        except ClientError as e:
            raise RuntimeError(f'Error deleting file from S3: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from S3 storage."""
        try:
            response = self.s3_client.list_objects_v2(Bucket=self.bucket_name)
            if 'Contents' in response:
                for content in response['Contents']:
                    # Skip objects that were not uploaded from open-webui in the first place
                    if not content['Key'].startswith(self.key_prefix):
                        continue

                    self.s3_client.delete_object(Bucket=self.bucket_name, Key=content['Key'])
        except ClientError as e:
            raise RuntimeError(f'Error deleting all files from S3: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()

    # The s3 key is the name assigned to an object. It excludes the bucket name, but includes the internal path and the file name.
    def _extract_s3_key(self, full_file_path: str) -> str:
        return '/'.join(full_file_path.split('//')[1].split('/')[1:])

    def _get_local_file_path(self, s3_key: str) -> str:
        return os.path.join(UPLOAD_DIR, s3_key.split('/')[-1])


class GCSStorageProvider(StorageProvider):
    def __init__(self):
        self.bucket_name = GCS_BUCKET_NAME

        if GOOGLE_APPLICATION_CREDENTIALS_JSON:
            self.gcs_client = storage.Client.from_service_account_info(
                info=JSONCodec.loads(GOOGLE_APPLICATION_CREDENTIALS_JSON)
            )
        else:
            # if no credentials json is provided, credentials will be picked up from the environment
            # if running on local environment, credentials would be user credentials
            # if running on a Compute Engine instance, credentials would be from Google Metadata server
            self.gcs_client = storage.Client()
        self.bucket = self.gcs_client.bucket(GCS_BUCKET_NAME)

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        """Handles uploading of the file to GCS storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        try:
            blob = self.bucket.blob(filename)
            blob.upload_from_filename(file_path)
            return contents, 'gs://' + self.bucket_name + '/' + filename
        except GoogleCloudError as e:
            raise RuntimeError(f'Error uploading file to GCS: {e}')

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from GCS storage."""
        try:
            filename = file_path.removeprefix('gs://').split('/')[1]
            local_file_path = os.path.join(UPLOAD_DIR, filename)
            blob = self.bucket.get_blob(filename)
            blob.download_to_filename(local_file_path)

            return local_file_path
        except NotFound as e:
            raise RuntimeError(f'Error downloading file from GCS: {e}')

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from GCS storage."""
        try:
            filename = file_path.removeprefix('gs://').split('/')[1]
            blob = self.bucket.get_blob(filename)
            blob.delete()
        except NotFound as e:
            raise RuntimeError(f'Error deleting file from GCS: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from GCS storage."""
        try:
            blobs = self.bucket.list_blobs()

            for blob in blobs:
                blob.delete()

        except NotFound as e:
            raise RuntimeError(f'Error deleting all files from GCS: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()


class AzureStorageProvider(StorageProvider):
    def __init__(self):
        self.endpoint = AZURE_STORAGE_ENDPOINT
        self.container_name = AZURE_STORAGE_CONTAINER_NAME
        storage_key = AZURE_STORAGE_KEY

        if storage_key:
            # Configure using the Azure Storage Account Endpoint and Key
            self.blob_service_client = BlobServiceClient(account_url=self.endpoint, credential=storage_key)
        else:
            # Configure using the Azure Storage Account Endpoint and DefaultAzureCredential
            # If the key is not configured, then the DefaultAzureCredential will be used to support Managed Identity authentication
            self.blob_service_client = BlobServiceClient(account_url=self.endpoint, credential=DefaultAzureCredential())
        self.container_client = self.blob_service_client.get_container_client(self.container_name)

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        """Handles uploading of the file to Azure Blob Storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        try:
            blob_client = self.container_client.get_blob_client(filename)
            blob_client.upload_blob(contents, overwrite=True)
            return contents, f'{self.endpoint}/{self.container_name}/{filename}'
        except Exception as e:
            raise RuntimeError(f'Error uploading file to Azure Blob Storage: {e}')

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from Azure Blob Storage."""
        try:
            filename = file_path.split('/')[-1]
            local_file_path = os.path.join(UPLOAD_DIR, filename)
            blob_client = self.container_client.get_blob_client(filename)
            with open(local_file_path, 'wb') as download_file:
                download_file.write(blob_client.download_blob().readall())
            return local_file_path
        except ResourceNotFoundError as e:
            raise RuntimeError(f'Error downloading file from Azure Blob Storage: {e}')

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from Azure Blob Storage."""
        try:
            filename = file_path.split('/')[-1]
            blob_client = self.container_client.get_blob_client(filename)
            blob_client.delete_blob()
        except ResourceNotFoundError as e:
            raise RuntimeError(f'Error deleting file from Azure Blob Storage: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from Azure Blob Storage."""
        try:
            blobs = self.container_client.list_blobs()
            for blob in blobs:
                self.container_client.delete_blob(blob.name)
        except Exception as e:
            raise RuntimeError(f'Error deleting all files from Azure Blob Storage: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()


def get_storage_provider(storage_provider: str):
    if USE_SLIM and storage_provider != 'local':
        raise RuntimeError(
            'Slim requires local file storage. Set STORAGE_PROVIDER=local, or use the standard image to access cloud storage.'
        )
    if storage_provider == 'local':
        Storage = LocalStorageProvider()
    elif storage_provider == 's3':
        Storage = S3StorageProvider()
    elif storage_provider == 'gcs':
        Storage = GCSStorageProvider()
    elif storage_provider == 'azure':
        Storage = AzureStorageProvider()
    else:
        raise RuntimeError(f'Unsupported storage provider: {storage_provider}')
    return Storage


class EncryptedStorageProvider(StorageProvider):
    """
    Wraps any storage provider and encrypts file contents before they reach it.

    Writes are encrypted only when encryption is enabled. Reads detect encrypted
    files by their header, so plaintext files written before encryption was
    enabled (and encrypted files after it was disabled) stay readable.

    Callers must read stored files through read_bytes, open_range or
    local_plaintext_path; get_file returns the raw (possibly encrypted) path.
    """

    def __init__(self, inner: StorageProvider, cipher: Optional[FileCipher], encrypt_writes: bool, temp_dir: str):
        if encrypt_writes and (cipher is None or not cipher.active_kid):
            raise RuntimeError(
                'ENABLE_FILE_ENCRYPTION is set but no active key is configured. '
                'Set FILE_ENCRYPTION_KEYS and FILE_ENCRYPTION_ACTIVE_KEY_ID.'
            )
        self.inner = inner
        self.cipher = cipher
        self.encrypt_writes = encrypt_writes
        self.temp_dir = temp_dir
        self._sweep_stale_temp_dirs()

    def _sweep_stale_temp_dirs(self, max_age_seconds: int = 3600) -> None:
        """Remove decrypted work dirs left behind by a crash. Recent ones may belong to other workers."""
        if not os.path.isdir(self.temp_dir):
            return
        cutoff = time.time() - max_age_seconds
        for entry in os.scandir(self.temp_dir):
            try:
                if entry.is_dir(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                pass

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
        payload = self.cipher.encrypt_bytes(contents) if self.encrypt_writes else contents
        _, file_path = self.inner.upload_file(io.BytesIO(payload), filename, tags)
        # Callers hash and index the returned contents, so hand back the plaintext.
        return contents, file_path

    def get_file(self, file_path: str) -> str:
        return self.inner.get_file(file_path)

    def delete_file(self, file_path: str) -> None:
        self.inner.delete_file(file_path)

    def delete_all_files(self) -> None:
        self.inner.delete_all_files()

    def _require_cipher(self) -> FileCipher:
        if self.cipher is None:
            raise FileEncryptionError('File is encrypted but FILE_ENCRYPTION_KEYS is not configured')
        return self.cipher

    @staticmethod
    def _is_encrypted_file(f: BinaryIO) -> bool:
        encrypted = is_encrypted(f.read(len(MAGIC)))
        f.seek(0)
        return encrypted

    def read_bytes(self, file_path: str, limit: Optional[int] = None) -> bytes:
        """Return the plaintext contents (optionally only the first `limit` bytes)."""
        local_path = self.inner.get_file(file_path)
        with open(local_path, 'rb') as f:
            if not self._is_encrypted_file(f):
                return f.read() if limit is None else f.read(limit)
            end = None if limit is None else limit - 1
            return b''.join(self._require_cipher().decrypt_range(f, 0, end))

    def open_range(self, file_path: str) -> Tuple[int, bool, str]:
        """Return (plaintext_size, is_encrypted, local_path) for serving the file."""
        local_path = self.inner.get_file(file_path)
        with open(local_path, 'rb') as f:
            if not self._is_encrypted_file(f):
                return os.path.getsize(local_path), False, local_path
            return self._require_cipher().plaintext_size(f), True, local_path

    def iter_range(self, local_path: str, start: int = 0, end: Optional[int] = None) -> Iterator[bytes]:
        """Yield decrypted bytes [start, end] of an encrypted local file (end inclusive)."""
        with open(local_path, 'rb') as f:
            yield from self._require_cipher().decrypt_range(f, start, end)

    @contextmanager
    def local_plaintext_path(self, file_path: str) -> Iterator[str]:
        """
        Yield a local path with plaintext contents for consumers that need a real file
        (document loaders, transcription). Decrypted copies live in a private per-call
        temp dir that is removed on exit, together with anything consumers wrote next
        to the file (e.g. converted or compressed audio).
        """
        local_path = self.inner.get_file(file_path)
        with open(local_path, 'rb') as f:
            encrypted = self._is_encrypted_file(f)
        if not encrypted:
            yield local_path
            return

        os.makedirs(self.temp_dir, mode=0o700, exist_ok=True)
        work_dir = tempfile.mkdtemp(dir=self.temp_dir)
        try:
            tmp_path = os.path.join(work_dir, os.path.basename(local_path))
            with open(tmp_path, 'wb') as out, open(local_path, 'rb') as f:
                for chunk in self._require_cipher().decrypt_range(f):
                    out.write(chunk)
            yield tmp_path
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @asynccontextmanager
    async def alocal_plaintext_path(self, file_path: str) -> AsyncIterator[str]:
        """Async variant of local_plaintext_path; download/decrypt/cleanup run in a thread."""
        cm = self.local_plaintext_path(file_path)
        path = await asyncio.to_thread(cm.__enter__)
        try:
            yield path
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)


def get_file_cipher() -> Optional[FileCipher]:
    keys = parse_keys(FILE_ENCRYPTION_KEYS)
    if not keys:
        return None
    return FileCipher(keys, FILE_ENCRYPTION_ACTIVE_KEY_ID or None)


Storage = EncryptedStorageProvider(
    get_storage_provider(STORAGE_PROVIDER),
    cipher=get_file_cipher(),
    encrypt_writes=ENABLE_FILE_ENCRYPTION,
    temp_dir=FILE_ENCRYPTION_TEMP_DIR,
)
