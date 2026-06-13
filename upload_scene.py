"""Upload an already-built out/scene_cad.zip and translate it (re-run of run.py's APS tail)."""
import os
import config
from aps.auth import get_token
from aps.upload import ensure_bucket, upload_object
from aps.translate import start_translation, wait_until_done

tok = get_token()
bucket = os.environ.get("APS_BUCKET", f"cb-{config.APS_CLIENT_ID[:12].lower()}")
ensure_bucket(tok, bucket)
object_key = os.environ.get("SCENE_KEY", "scene_cad_v7.zip")
urn = upload_object(tok, bucket, object_key, "out/scene_cad.zip")
start_translation(tok, urn, root_filename="scene.obj")
wait_until_done(tok, urn)
print("\nPASTE THIS URN INTO viewer/index.html:\n", urn)
