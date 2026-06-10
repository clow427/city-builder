import glob
import pyproj
import config
from pipeline.crop import load_points, las_crs
from pipeline.segment import (ground_mask_pdal, split_ground,
                              classify_road_sidewalk, load_road_lines,
                              cluster_objects, keep_car_clusters)
from pipeline.lift_assets import lift_assets
from pipeline.classify_io import write_colored_las
from pipeline.to_cad import write_obj, cars_to_objects, assets_to_objects
from aps.auth import get_token
from aps.upload import ensure_bucket, upload_object
from aps.translate import start_translation, wait_until_done

LAS = glob.glob(config.LAZ_DIR + "/*.laz")[0]
crs = las_crs(LAS)

# reproject DEFAULT_BBOX (WGS84) corners into the LAS CRS -> projected bbox
tf = pyproj.Transformer.from_crs(4326, crs, always_xy=True)
minx, miny = tf.transform(config.DEFAULT_BBOX[0], config.DEFAULT_BBOX[1])
maxx, maxy = tf.transform(config.DEFAULT_BBOX[2], config.DEFAULT_BBOX[3])
bbox_proj = (minx, miny, maxx, maxy)

pts, rgb, _ = load_points()
gmask = ground_mask_pdal(LAS)
ground, nonground = split_ground(pts, gmask)

road_lines = load_road_lines("data/pavements_v2.geojson", bbox_proj, 4326, crs)
labels = classify_road_sidewalk(ground, road_lines, width_m=7.0)

cars = keep_car_clusters(cluster_objects(nonground))
assets = lift_assets(pts, "data/aboveGroundAssets_v2.geojson", 4326, crs, bbox_proj)

write_colored_las("out/classified.laz", ground, labels)
write_obj("out/scene.obj", cars_to_objects(cars) + assets_to_objects(assets))

tok = get_token()
bucket = "cyvl-hack-xavier"
ensure_bucket(tok, bucket)
urn = upload_object(tok, bucket, "scene.obj", "out/scene.obj")
start_translation(tok, urn)
wait_until_done(tok, urn)
print("PASTE THIS URN INTO viewer/index.html:", urn)
