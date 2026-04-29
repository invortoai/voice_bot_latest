"""
s3_service.py — Centralised S3 presigning and upload utility.

Converts ``s3://bucket/key`` URIs into time-limited presigned HTTPS URLs.
Non-S3 URLs (Twilio / MCube recording links) are returned unchanged.

Also provides ``upload_file_to_s3`` for streaming uploads (used by the
direct file-upload endpoint POST /insights/analyse/upload).
"""

from typing import Optional
from urllib.parse import urlparse

import boto3
from loguru import logger

from app.config import (
    S3_ACCESS_KEY_ID,
    S3_BUCKET_NAME,
    S3_PRESIGNED_URL_EXPIRY,
    S3_REGION,
    S3_SECRET_ACCESS_KEY,
)

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=S3_REGION,
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        )
    return _s3_client


def presign_recording_url(
    url: Optional[str], expiry: int = S3_PRESIGNED_URL_EXPIRY
) -> Optional[str]:
    """Return a presigned URL for an S3 URI, or pass through non-S3 URLs."""
    if not url:
        return None
    if not url.startswith("s3://"):
        return url  # Twilio / MCube URLs are already accessible

    parsed = urlparse(url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")

    try:
        return _get_s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiry,
        )
    except Exception as e:
        logger.error(f"Failed to presign S3 URL {url}: {e}")
        return url  # fall back to raw URI rather than losing the link


def upload_file_to_s3(
    file_obj,
    s3_key: str,
    content_type: str,
    bucket: str | None = None,
) -> str:
    """Stream-upload a file-like object to S3.

    Uses boto3 upload_fileobj which streams in chunks (default 8 MB) — no
    full-file RAM buffer.  Caller must seek(0) before passing file_obj if any
    bytes have been read (e.g. for magic-byte validation).

    Args:
        file_obj:     BinaryIO — e.g. SpooledTemporaryFile from FastAPI UploadFile.
        s3_key:       Key within the bucket, e.g. "file_uploads/org_id/job_id.wav".
        content_type: MIME type stored as S3 object metadata.
        bucket:       Target bucket; defaults to S3_BUCKET_NAME from config.

    Returns:
        s3://bucket/key URI on success.

    Raises:
        botocore.exceptions.ClientError on S3 failure.
    """
    target_bucket = bucket or S3_BUCKET_NAME
    _get_s3_client().upload_fileobj(
        file_obj,
        target_bucket,
        s3_key,
        ExtraArgs={"ContentType": content_type},
    )
    return f"s3://{target_bucket}/{s3_key}"
