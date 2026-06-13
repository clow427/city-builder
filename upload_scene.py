"""Upload an already-built out/<block_id>/scene_cad.zip and translate it.

Usage:
    BLOCK_ID=elm_st python upload_scene.py
"""
import json
import os
import config
from aps.auth import get_token
from aps.upload import ensure_bucket, upload_object
from aps.translate import start_translation, wait_until_done

block_id = os.environ.get("BLOCK_ID", "davis_sq_a")
OUT_DIR = os.path.join("out", block_id)
tok = get_token()
bucket = os.environ.get("APS_BUCKET", f"cb-{config.APS_CLIENT_ID[:12].lower()}")
ensure_bucket(tok, bucket)
object_key = os.environ.get("SCENE_KEY", f"scene_{block_id}.zip")
urn = upload_object(tok, bucket, object_key, os.path.join(OUT_DIR, "scene_cad.zip"))
start_translation(tok, urn, root_filename="scene.obj")
wait_until_done(tok, urn)
print(f"\nScene '{block_id}' URN: {urn}")

scenes_path = os.path.join("viewer", "scenes.json")
scenes = json.load(open(scenes_path)) if os.path.exists(scenes_path) else []
label = os.environ.get("SCENE_LABEL", block_id.replace("_", " ").title())
entry = {"id": block_id, "label": label, "urn": urn, "data_dir": OUT_DIR}
scenes = [s for s in scenes if s.get("id") != block_id]
scenes.append(entry)
json.dump(scenes, open(scenes_path, "w"), indent=2)
print(f"Registered '{block_id}' in viewer/scenes.json")
