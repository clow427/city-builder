"""Dummy APS round-trip: two car boxes -> OBJ -> bucket -> SVF2 translate -> URN."""
from pipeline.to_cad import write_dummy_scene
from aps.auth import get_token
from aps.upload import ensure_bucket, upload_object
from aps.translate import start_translation, wait_until_done
from config import APS_CLIENT_ID

write_dummy_scene("out/scene.obj")

tok = get_token()
bucket = f"cb-dummy-{APS_CLIENT_ID[:12].lower()}"
ensure_bucket(tok, bucket)
urn = upload_object(tok, bucket, "scene_dummy.obj", "out/scene.obj")
start_translation(tok, urn)
wait_until_done(tok, urn)
print("\nPASTE THIS URN INTO viewer/index.html:\n", urn)
