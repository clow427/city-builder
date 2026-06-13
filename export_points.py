"""Export the ROI as a binary colored point cloud for the APS Viewer overlay.

Output viewer/points.bin layout (little-endian):
  uint32  count
  float32 count*3  positions (x - ox, y - oy, z)   -- same local origin as scene.obj
  uint8   count*3  colors (RGB)

Run in the conda `hack` env with LAZ_DIR (+ optional ROI_CX/ROI_CY) set,
same as run.py, so the points line up with the uploaded CAD scene.
"""
import glob
import json
import os
import struct
import numpy as np
import laspy
import pdal
import config

ROI_M = float(os.environ.get("ROI_M", 80.0))
VOXEL = 0.15
MAX_POINTS = 2_500_000

block_id = os.environ.get("BLOCK_ID") or os.path.splitext(
    os.path.basename(glob.glob(config.LAZ_DIR + "/*.laz")[0]))[0]
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", block_id)
os.makedirs(OUT_DIR, exist_ok=True)

LAS = glob.glob(config.LAZ_DIR + "/*.laz")[0]
with laspy.open(LAS) as f:
    h = f.header
    cx = (h.mins[0] + h.maxs[0]) / 2
    cy = (h.mins[1] + h.maxs[1]) / 2
cx = float(os.environ.get("ROI_CX", cx))
cy = float(os.environ.get("ROI_CY", cy))
bbox = (cx - ROI_M / 2, cy - ROI_M / 2, cx + ROI_M / 2, cy + ROI_M / 2)
print("ROI bbox:", bbox)

pl = pdal.Pipeline(json.dumps({"pipeline": [
    LAS,
    {"type": "filters.crop",
     "bounds": f"([{bbox[0]},{bbox[2]}],[{bbox[1]},{bbox[3]}])"},
]}))
pl.execute()
arr = pl.arrays[0]
xyz = np.column_stack([arr["X"], arr["Y"], arr["Z"]]).astype(np.float64)
inten = arr["Intensity"].astype(np.float64)
print(f"ROI points: {len(xyz)}")

# voxel downsample (keep first point per voxel, intensity follows)
keys = np.floor(xyz / VOXEL).astype(np.int64)
_, idx = np.unique(keys, axis=0, return_index=True)
idx = np.sort(idx)
xyz, inten = xyz[idx], inten[idx]
print(f"after voxel {VOXEL}: {len(xyz)}")

if len(xyz) > MAX_POINTS:
    sel = np.random.default_rng(0).choice(len(xyz), MAX_POINTS, replace=False)
    sel.sort()
    xyz, inten = xyz[sel], inten[sel]
    print(f"capped to {MAX_POINTS}")

# This tile carries no RGB (fields all zero), so synthesize color:
# height ramp (turbo colormap) modulated by LiDAR intensity.
from matplotlib import colormaps
z = xyz[:, 2]
zlo, zhi = np.percentile(z, 2), np.percentile(z, 98)
hnorm = np.clip((z - zlo) / max(zhi - zlo, 1e-6), 0, 1)
ilo, ihi = np.percentile(inten, 2), np.percentile(inten, 98)
inorm = np.clip((inten - ilo) / max(ihi - ilo, 1e-6), 0, 1)
ramp = colormaps["turbo"](hnorm)[:, :3]
rgb = ramp * (0.35 + 0.65 * inorm[:, None]) * 255
rgb = np.clip(rgb, 0, 255).astype(np.uint8)

# same local origin as run.py applies to scene.obj (z untouched)
xyz[:, 0] -= bbox[0]
xyz[:, 1] -= bbox[1]
pos = xyz.astype(np.float32)

out = os.path.join(OUT_DIR, "points.bin")
with open(out, "wb") as f:
    f.write(struct.pack("<I", len(pos)))
    f.write(pos.tobytes())
    f.write(rgb.tobytes())
print(f"wrote {out}  ({os.path.getsize(out)/1e6:.1f} MB, {len(pos)} points)")
