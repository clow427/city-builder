"""Turn classified ground points into a solid triangulated heightfield mesh.

Grid the ROI into square cells, take mean ground z per cell, fill empty cells
from the nearest filled cell, then emit one quad per cell grouped into OBJ
objects by surface material (road -> asphalt, sidewalk -> concrete, grass).

When `road_bins` is supplied (a per-cell pavement-condition bin from
pipeline.pavement, flattened ix*ny+iy like the rest of the grid), road cells are
split into `ground_road_good/fair/poor` objects with the green->red condition
materials, so each bin is a selectable, recolorable dbId in the viewer. Road
cells with no condition match fall back to a plain `ground_road` (asphalt).
"""
import numpy as np
from scipy.spatial import cKDTree

ROAD_BIN_MATERIALS = [
    ("good", "road_good", "ground_road_good"),
    ("fair", "road_fair", "ground_road_fair"),
    ("poor", "road_poor", "ground_road_poor"),
]


def _quad_object(name, material, mask, xmin, ymin, cell, vz):
    """Build one OBJ object from a boolean (nx, ny) cell mask, or None if empty."""
    nx, ny = mask.shape
    verts, vmap, faces = [], {}, []

    def vid(i, j):
        key = (i, j)
        if key not in vmap:
            vmap[key] = len(verts)
            verts.append((xmin + i * cell, ymin + j * cell, vz[i, j]))
        return vmap[key]

    for i in range(nx):
        for j in range(ny):
            if mask[i, j]:
                faces.append((vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)))
    if not faces:
        return None
    return {"name": name, "material": material, "verts": verts, "faces": faces}


def ground_grid_mesh(ground_pts, labels, bbox, cell=0.5, road_bins=None):
    xmin, ymin, xmax, ymax = bbox
    nx = max(int(np.ceil((xmax - xmin) / cell)), 1)
    ny = max(int(np.ceil((ymax - ymin) / cell)), 1)

    ix = np.clip(((ground_pts[:, 0] - xmin) / cell).astype(int), 0, nx - 1)
    iy = np.clip(((ground_pts[:, 1] - ymin) / cell).astype(int), 0, ny - 1)
    flat = ix * ny + iy

    CLASSES = ["road", "sidewalk", "grass"]
    zsum = np.zeros(nx * ny)
    cnt = np.zeros(nx * ny)
    cls_cnt = np.zeros((3, nx * ny))
    np.add.at(zsum, flat, ground_pts[:, 2])
    np.add.at(cnt, flat, 1)
    lab_idx = np.array([CLASSES.index(l) if l in CLASSES else 2 for l in labels])
    for c in range(3):
        np.add.at(cls_cnt[c], flat[lab_idx == c], 1)

    filled = cnt > 0
    zcell = np.full(nx * ny, np.nan)
    zcell[filled] = zsum[filled] / cnt[filled]
    matcell = np.full(nx * ny, 2)
    matcell[filled] = cls_cnt[:, filled].argmax(axis=0)

    # fill holes from nearest filled cell (z and material)
    ci, cj = np.divmod(np.arange(nx * ny), ny)
    centers = np.column_stack([xmin + (ci + 0.5) * cell, ymin + (cj + 0.5) * cell])
    if not filled.all():
        tree = cKDTree(centers[filled])
        _, nn = tree.query(centers[~filled])
        zcell[~filled] = zcell[filled][nn]
        matcell[~filled] = matcell[filled][nn]

    # sidewalks read as a raised slab (crisp curb edge at 0.5m cells)
    zcell[matcell == 1] += 0.12

    # vertex z = mean of the (up to 4) adjacent cell z values
    zgrid = zcell.reshape(nx, ny)
    vz = np.zeros((nx + 1, ny + 1))
    wsum = np.zeros((nx + 1, ny + 1))
    for dx in (0, 1):
        for dy in (0, 1):
            vz[dx:nx + dx, dy:ny + dy] += zgrid
            wsum[dx:nx + dx, dy:ny + dy] += 1
    vz /= wsum

    objs = []
    matgrid = matcell.reshape(nx, ny)
    road_mask = matgrid == 0

    if road_bins is not None:
        binsgrid = np.asarray(road_bins, dtype=object).reshape(nx, ny)
        for bin_name, mat, obj_name in ROAD_BIN_MATERIALS:
            o = _quad_object(obj_name, mat, road_mask & (binsgrid == bin_name),
                             xmin, ymin, cell, vz)
            if o:
                objs.append(o)
        known = np.isin(binsgrid, [b for b, _, _ in ROAD_BIN_MATERIALS])
        unbinned = _quad_object("ground_road", "asphalt", road_mask & ~known,
                                xmin, ymin, cell, vz)
        if unbinned:
            objs.append(unbinned)
    else:
        o = _quad_object("ground_road", "asphalt", road_mask, xmin, ymin, cell, vz)
        if o:
            objs.append(o)

    for ci_, mat, name in [(1, "concrete", "ground_sidewalk"),
                           (2, "grass", "ground_grass")]:
        o = _quad_object(name, mat, matgrid == ci_, xmin, ymin, cell, vz)
        if o:
            objs.append(o)
    return objs
