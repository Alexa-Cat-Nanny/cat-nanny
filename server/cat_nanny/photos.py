"""Photo files: where the bytes live and how a phone gets them there.

Ported from CatNannyFramework photo_service (type sniffing, 8 MB cap). Two backends:
- `LocalPhotoStorage`: files on disk, served by this server (local development).
- `S3PhotoStorage`: objects in S3; the phone uploads straight to S3 with a
  presigned PUT, so photos never pass through Lambda's 6 MB payload limit.

Both expose the same three operations: an upload target for the browser, a
view URL for a stored key, and a sniff of the file type.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MAX_PHOTO_BYTES = 8 * 1024 * 1024
ALLOWED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class PhotoValidationError(ValueError):
    pass


def sniff_content_type(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    raise PhotoValidationError("Please choose a JPEG, PNG, or WebP image.")


def photo_key(household: str, event_id: str, photo_id: str, content_type: str) -> str:
    return f"households/{household}/events/{event_id}/{photo_id}{ALLOWED[content_type]}"


@dataclass
class UploadTarget:
    """Where the browser should PUT the bytes, and the key the server will record."""

    url: str
    method: str
    headers: dict[str, str]
    storage_key: str


class PhotoStorage(Protocol):
    def upload_target(self, storage_key: str, content_type: str, public_base_url: str) -> UploadTarget: ...
    def view_url(self, storage_key: str, public_base_url: str) -> str: ...
    def exists(self, storage_key: str) -> bool: ...


class LocalPhotoStorage:
    """Writes go to PUT /photos/blob/<key> on this server; reads come from GET /photos/blob/<key>."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, storage_key: str) -> Path:
        p = (self.root / storage_key).resolve()
        if self.root.resolve() not in p.parents:
            raise PhotoValidationError("Invalid photo key.")
        return p

    def upload_target(self, storage_key, content_type, public_base_url):
        return UploadTarget(url=f"{public_base_url}/photos/blob/{storage_key}", method="PUT",
                            headers={"Content-Type": content_type}, storage_key=storage_key)

    def view_url(self, storage_key, public_base_url):
        return f"{public_base_url}/photos/blob/{storage_key}"

    def exists(self, storage_key):
        return self.path(storage_key).is_file()

    def write(self, storage_key: str, data: bytes) -> None:
        p = self.path(storage_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read(self, storage_key: str) -> bytes:
        return self.path(storage_key).read_bytes()


class S3PhotoStorage:
    def __init__(self, bucket: str, client: Any = None, url_ttl_seconds: int = 3600) -> None:
        if client is None:
            import boto3
            from botocore.config import Config

            # Signature V4 is required for presigned URLs on buckets created after 2020 (V2 is rejected).
            client = boto3.client("s3", config=Config(signature_version="s3v4"))
        self._s3 = client
        self.bucket = bucket
        self.ttl = url_ttl_seconds

    def upload_target(self, storage_key, content_type, public_base_url):
        url = self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": storage_key, "ContentType": content_type},
            ExpiresIn=self.ttl,
        )
        return UploadTarget(url=url, method="PUT", headers={"Content-Type": content_type}, storage_key=storage_key)

    def view_url(self, storage_key, public_base_url):
        return self._s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": storage_key}, ExpiresIn=self.ttl)

    def exists(self, storage_key):
        try:
            self._s3.head_object(Bucket=self.bucket, Key=storage_key)
            return True
        except self._s3.exceptions.ClientError:
            return False


def make_photo_storage(kind: str, *, bucket: str = "", root: Path | None = None) -> PhotoStorage:
    if kind == "s3":
        if not bucket:
            raise ValueError("PHOTOS=s3 needs PHOTO_BUCKET")
        return S3PhotoStorage(bucket)
    if kind == "local":
        return LocalPhotoStorage(root or Path("uploads"))
    raise ValueError(f"Unknown PHOTOS {kind!r} (use local or s3)")
