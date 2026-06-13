"""Diagnostic v3: car detection with real dimensions + ground contact + aspect ratio.
Run in the hack env. Not part of the pipeline."""
import glob
import json
import numpy as np
import laspy
import pdal
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import config
from pipeline.segment import split_ground

ROI_M = 80.0
VOXEL = 0.10
# car dimension gates (metres), grounded in standard vehicle sizes
L_LO, L_HI = 3.5, 6.3
W_LO, W_HI = 1.2, 2.4
H_LO, H_HI = 1.0, 2.3          # vertical extent (above ground)
ASPECT_LO, ASPECT_HI = 1.7, 4.5
GROUND_TOUCH = 0.5            # lowest cluster point must be within this of ground

LAS = glob.glob(config.LAZ_DIR + "/*.laz")[0]
with laspy.open(LAS) as f:
    hh = f.header
    cx = (hh.mins[0] + hh.maxs[0]) / 2
    cy = (hh.mins[1] + hh.maxs[1]) / 2
bb = (cx - ROI_M / 2, cy - ROI_M / 2, cx + ROI_M / 2, cy + ROI_M / 2)

pl = pdal.Pipeline(json.dumps({"pipeline": [
    LAS,
    {"type": "filters.crop", "bounds": f"([{bb[0]},{bb[2]}],[{bb[1]},{bb[3]}])"},
    {"type": "filters.smrf"},
]}))
pl.execute()
arr = pl.arrays[0]
xyz = np.column_stack([arr["X"], arr["Y"], arr["Z"]]).astype(float)
gmask = arr["Classification"] == 2
ground, nonground = split_ground(xyz, gmask)


def vox(p, v):
    k = np.floor(p[:, :3] / v).astype(np.int64)
    _, idx = np.unique(k, axis=0, return_index=True)
    return p[np.sort(idx)]


g = vox(ground, VOXEL)
ng = vox(nonground, VOXEL)
for a in (g, ng):
    a[:, 0] -= bb[0]; a[:, 1] -= bb[1]

# height above local ground for each non-ground point
gt = cKDTree(g[:, :2])
_, gi = gt.query(ng[:, :2], k=1)
h_above = ng[:, 2] - g[gi, 2]

# cluster the object band (near-ground up to a bit above car height) by footprint
m = (h_above > 0.1) & (h_above < 2.6)
band = ng[m]; band_h = h_above[m]
lbl = DBSCAN(eps=0.5, min_samples=12).fit_predict(band[:, :2])

fig, ax = plt.subplots(figsize=(11, 11))
ax.scatter(g[:, 0], g[:, 1], s=0.2, c="lightgray")
rng = np.random.default_rng(1)
cars = 0
for k in set(lbl):
    if k == -1:
        continue
    sel = lbl == k
    c = band[sel]; ch = band_h[sel]
    mins, maxs = c[:, :2].min(0), c[:, :2].max(0)
    dims = sorted(maxs - mins)
    W, L = float(dims[0]), float(dims[1])
    zmin_ab, zmax_ab = float(ch.min()), float(ch.max())
    aspect = L / W if W > 0 else 99
    is_car = (L_LO <= L <= L_HI and W_LO <= W <= W_HI and
              H_LO <= zmax_ab <= H_HI and zmin_ab <= GROUND_TOUCH and
              ASPECT_LO <= aspect <= ASPECT_HI)
    ax.scatter(c[:, 0], c[:, 1], s=1.0, color=rng.random(3))
    if is_car:
        cars += 1
        ax.add_patch(Rectangle((mins[0], mins[1]), maxs[0] - mins[0], maxs[1] - mins[1],
                     fill=False, edgecolor="red", linewidth=2))
        ax.text(mins[0], maxs[1], f"{L:.1f}x{W:.1f}", fontsize=6, color="red")
ax.set_title(f"cars (real dims + ground-touch + aspect) = {cars}")
ax.set_aspect("equal")
plt.savefig("out/debug3.png", dpi=95, bbox_inches="tight")
print("CARS:", cars)

# diagnostics: distribution of plausible vehicle-ish clusters (L in 2..8 m)
print("\nL    W    aspc zmin zmax  npts  -> fail")
for k in set(lbl):
    if k == -1:
        continue
    sel = lbl == k
    c = band[sel]; ch = band_h[sel]
    mins, maxs = c[:, :2].min(0), c[:, :2].max(0)
    dims = sorted(maxs - mins)
    W, L = float(dims[0]), float(dims[1])
    if not (2.0 <= L <= 8.0):
        continue
    zmin_ab, zmax_ab = float(ch.min()), float(ch.max())
    asp = L / W if W > 0 else 99
    fails = []
    if not (L_LO <= L <= L_HI): fails.append("L")
    if not (W_LO <= W <= W_HI): fails.append("W")
    if not (H_LO <= zmax_ab <= H_HI): fails.append("Hmax")
    if zmin_ab > GROUND_TOUCH: fails.append("grnd")
    if not (ASPECT_LO <= asp <= ASPECT_HI): fails.append("asp")
    print(f"{L:4.1f} {W:4.1f} {asp:4.1f} {zmin_ab:4.1f} {zmax_ab:4.1f} {len(c):5d}  -> {','.join(fails) or 'CAR'}")
