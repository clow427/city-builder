"""Turn classified ground points into a solid triangulated heightfield mesh.

Grid the ROI into square cells, take mean ground z per cell, fill empty cells
from the nearest filled cell, then emit one quad per cell grouped into two OBJ
objects by surface material (road -> asphalt, everything else -> concrete).
"""
import numpy as np
from scipy.spatial import cKDTree


def ground_grid_mesh(ground_pts, labels, bbox, cell=0.5):
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
    for ci_, (mat, name) in enumerate([("asphalt", "ground_road"),
                                       ("concrete", "ground_sidewalk"),
                                       ("grass", "ground_grass")]):
        mask = matgrid == ci_
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
        if faces:
            objs.append({"name": name, "material": mat, "verts": verts, "faces": faces})
    return objs
