"""S3PhotoStorage against a mocked bucket (moto): presigned upload target, existence check, view URL."""

import boto3
import pytest
from moto import mock_aws

from cat_nanny.photos import S3PhotoStorage, photo_key


@pytest.fixture
def storage():
    with mock_aws():
        boto3.setup_default_session(region_name="us-east-1")
        store = S3PhotoStorage("cat-nanny-photos-test")  # builds its own SigV4 client, like on Lambda
        store._s3.create_bucket(Bucket="cat-nanny-photos-test")
        yield store


def test_presigned_put_then_exists_then_presigned_get(storage):
    key = photo_key("demo", "ev-1", "photo-1", "image/jpeg")
    assert key == "households/demo/events/ev-1/photo-1.jpg"
    target = storage.upload_target(key, "image/jpeg", "ignored-for-s3")
    assert target.method == "PUT" and "X-Amz-Signature" in target.url and target.headers == {"Content-Type": "image/jpeg"}
    assert not storage.exists(key)

    storage._s3.put_object(Bucket=storage.bucket, Key=key, Body=b"\xff\xd8\xff", ContentType="image/jpeg")
    assert storage.exists(key)
    assert storage.view_url(key, "ignored-for-s3").startswith("https://") and key in storage.view_url(key, "")
