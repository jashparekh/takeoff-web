"""File storage: DigitalOcean Spaces in production, MinIO locally, both through the S3 API. The bucket is private;
browsers get short-lived presigned links.

Key scheme (docs/plans/upload-pipeline-implementation.md §2):
    p/{project_id}/u/{upload_id}/original.pdf
    p/{project_id}/s/{sheet_id}/display.png | thumb.png | ocr300.png
Deleting a project deletes everything under `p/{project_id}/`.
"""
from __future__ import annotations
from functools import lru_cache
from typing import BinaryIO, Protocol

from .settings import get_settings

PRESIGN_SECONDS = 600          # links to files live at most 10 minutes


def upload_key(project_id, upload_id) -> str:
    return f'p/{project_id}/u/{upload_id}/original.pdf'


def sheet_key(project_id, sheet_id, name: str) -> str:
    return f'p/{project_id}/s/{sheet_id}/{name}'


class Storage(Protocol):
    def put(self, key: str, data: bytes | BinaryIO, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def url(self, key: str) -> str: ...
    def delete_prefix(self, prefix: str) -> None: ...


class S3Storage:
    def __init__(self, endpoint: str | None, bucket: str, access_key: str | None, secret_key: str | None,
                 region: str | None = None):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self.s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access_key,
                               aws_secret_access_key=secret_key, region_name=region or 'us-east-1',
                               config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))

    def put(self, key, data, content_type):
        if isinstance(data, bytes):
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        else:
            self.s3.upload_fileobj(data, self.bucket, key, ExtraArgs={'ContentType': content_type})

    def get(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)['Body'].read()

    def url(self, key):
        return self.s3.generate_presigned_url('get_object', Params={'Bucket': self.bucket, 'Key': key},
                                              ExpiresIn=PRESIGN_SECONDS)

    def delete_prefix(self, prefix):
        pages = self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=prefix)
        for page in pages:
            keys = [{'Key': o['Key']} for o in page.get('Contents', [])]
            if keys:
                self.s3.delete_objects(Bucket=self.bucket, Delete={'Objects': keys})

    def ensure_bucket(self) -> None:
        """Local development only (MinIO): create the bucket if missing. On Spaces, create it in the dashboard."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except Exception:                                      # noqa: BLE001 — missing or not ours yet
            self.s3.create_bucket(Bucket=self.bucket)


class MemoryStorage:
    """In-process storage for tests."""
    def __init__(self):
        self.files: dict[str, tuple[bytes, str]] = {}

    def put(self, key, data, content_type):
        self.files[key] = (data if isinstance(data, bytes) else data.read(), content_type)

    def get(self, key):
        return self.files[key][0]

    def url(self, key):
        return f'memory://{key}'

    def delete_prefix(self, prefix):
        for k in [k for k in self.files if k.startswith(prefix)]:
            del self.files[k]


@lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if not s.s3_bucket:
        raise RuntimeError('S3_BUCKET is not set (see .env.example)')
    return S3Storage(s.s3_endpoint_url, s.s3_bucket, s.s3_access_key, s.s3_secret_key, s.s3_region)


if __name__ == '__main__':                                   # python -m app.storage → create the local MinIO bucket
    st = get_storage()
    st.ensure_bucket()
    print(f'bucket {get_settings().s3_bucket} ready')
