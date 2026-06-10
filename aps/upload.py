import base64
import requests
from config import APS_HOST


def ensure_bucket(token, bucket_key):
    r = requests.post(
        f"{APS_HOST}/oss/v2/buckets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"bucketKey": bucket_key, "policyKey": "transient"},
        timeout=30,
    )
    if r.status_code not in (200, 409):  # 409 = already exists
        r.raise_for_status()


def upload_object(token, bucket_key, object_key, file_path):
    """Signed-S3 upload: get URL -> PUT to S3 -> complete. Returns URN (base64 objectId)."""
    base = f"{APS_HOST}/oss/v2/buckets/{bucket_key}/objects/{object_key}"
    h = {"Authorization": f"Bearer {token}"}
    signed = requests.get(f"{base}/signeds3upload", headers=h, timeout=30).json()
    upload_key = signed["uploadKey"]
    put_url = signed["urls"][0]
    with open(file_path, "rb") as f:
        requests.put(put_url, data=f, timeout=120).raise_for_status()
    done = requests.post(
        f"{base}/signeds3upload",
        headers={**h, "Content-Type": "application/json"},
        json={"uploadKey": upload_key},
        timeout=30,
    )
    done.raise_for_status()
    object_id = done.json()["objectId"]
    return base64.urlsafe_b64encode(object_id.encode()).decode().rstrip("=")
