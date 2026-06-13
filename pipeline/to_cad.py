"""Build a CAD-looking OBJ/MTL scene from detected geometry.

Objects are generic meshes: {name, material, verts: [(x,y,z)], faces: [(i,...)]}
(face indices are 0-based within the object's own verts). write_obj() handles
the global vertex offsets and per-object `usemtl` lines.
"""
import math

MATERIALS = {
    "asphalt":  (0.13, 0.13, 0.15),
    # pavement-condition ramp (green -> amber -> red) for binned road cells
    "road_good": (0.20, 0.60, 0.24),
    "road_fair": (0.85, 0.65, 0.13),
    "road_poor": (0.70, 0.16, 0.14),
    "concrete": (0.80, 0.78, 0.72),
    "grass":    (0.36, 0.50, 0.28),
    "car":      (0.30, 0.38, 0.50),
    "glass":    (0.15, 0.18, 0.22),
    "trunk":    (0.36, 0.26, 0.18),
    "canopy":   (0.20, 0.45, 0.20),
    "pole":     (0.45, 0.45, 0.48),
    "hydrant":  (0.75, 0.15, 0.12),
    "metal":    (0.35, 0.35, 0.38),
    "signal":   (0.12, 0.12, 0.12),
    "misc":     (0.62, 0.58, 0.30),
    "wall":     (0.72, 0.66, 0.58),
    "roof":     (0.45, 0.40, 0.38),
}


def write_mtl(path):
    lines = []
    for name, (r, g, b) in MATERIALS.items():
        lines += [f"newmtl {name}", f"Kd {r:.3f} {g:.3f} {b:.3f}",
                  "Ka 0 0 0", "Ks 0.05 0.05 0.05", "Ns 10", ""]
    open(path, "w").write("\n".join(lines))


# ---------- primitives (verts 0-indexed, faces reference local verts) ----------

def mesh_box(center, size, yaw=0.0):
    cx, cy, cz = center
    sx, sy, sz = size
    c, s = math.cos(yaw), math.sin(yaw)
    verts = []
    for ox, oy, oz in [(-.5, -.5, -.5), (.5, -.5, -.5), (.5, .5, -.5), (-.5, .5, -.5),
                       (-.5, -.5, .5), (.5, -.5, .5), (.5, .5, .5), (-.5, .5, .5)]:
        x, y, z = ox * sx, oy * sy, oz * sz
        verts.append((cx + x * c - y * s, cy + x * s + y * c, cz + z))
    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return verts, faces


def mesh_cylinder(cx, cy, z0, radius, height, n=12):
    verts, faces = [], []
    for i in range(n):
        a = 2 * math.pi * i / n
        x, y = cx + radius * math.cos(a), cy + radius * math.sin(a)
        verts.append((x, y, z0))
        verts.append((x, y, z0 + height))
    for i in range(n):
        j = (i + 1) % n
        faces.append((2 * i, 2 * j, 2 * j + 1, 2 * i + 1))
    faces.append(tuple(2 * i for i in range(n)))          # bottom cap
    faces.append(tuple(2 * i + 1 for i in range(n)))      # top cap
    return verts, faces


def mesh_cone(cx, cy, z0, radius, height, n=12):
    verts = [(cx + radius * math.cos(2 * math.pi * i / n),
              cy + radius * math.sin(2 * math.pi * i / n), z0) for i in range(n)]
    verts.append((cx, cy, z0 + height))                   # apex
    faces = [(i, (i + 1) % n, n) for i in range(n)]
    faces.append(tuple(range(n)))                         # base cap
    return verts, faces


# ---------- scene generators ----------

def car_objects(cars):
    """cars: list of {center, length, width, height, yaw}. Body box + cabin box."""
    objs = []
    for i, b in enumerate(cars, 1):
        cx, cy, cz = b["center"]
        L, W, H = b["length"], b["width"], b["height"]
        yaw = b.get("yaw", 0.0)
        z0 = cz - H / 2
        bv, bf = mesh_box((cx, cy, z0 + 0.35 * H), (L, W, 0.7 * H), yaw)
        objs.append({"name": f"car_{i:02d}", "material": "car", "verts": bv,
                     "faces": bf, "anchor": [cx, cy, cz]})
        # cabin sits on the body, slightly rearward along the car axis
        off = -0.08 * L
        ox, oy = off * math.cos(yaw), off * math.sin(yaw)
        cv, cf = mesh_box((cx + ox, cy + oy, z0 + 0.78 * H), (0.55 * L, 0.85 * W, 0.44 * H), yaw)
        objs.append({"name": f"car_{i:02d}_cabin", "material": "glass", "verts": cv, "faces": cf})
    return objs


def asset_objects(assets):
    """assets: list of {type, x, y, ground_z, height}. Typed parametric models."""
    SKIP = {"CURB", "SIDEWALK", "RAMP", "GUARDRAILS"}   # covered by the ground mesh
    objs = []
    for i, a in enumerate(assets, 1):
        t = (a["type"] or "OTHER").upper()
        if t in SKIP:
            continue
        x, y, z0 = a["x"], a["y"], a["ground_z"]
        H = max(a["height"], 0.3)
        nm = f"{t.lower()}_{i:02d}"
        start = len(objs)            # tag the base object with its drag anchor
        if t == "TREE":
            tv, tf = mesh_cylinder(x, y, z0, 0.15, 0.40 * H)
            objs.append({"name": nm, "material": "trunk", "verts": tv, "faces": tf})
            cv, cf = mesh_cone(x, y, z0 + 0.35 * H, min(1.8, 0.3 * H + 0.8), 0.65 * H)
            objs.append({"name": nm + "_canopy", "material": "canopy", "verts": cv, "faces": cf})
        elif t in ("UTILITY_POLE", "TRAFFIC_SIGNAL_POLE", "LUMINARIES"):
            pv, pf = mesh_cylinder(x, y, z0, 0.22, H)
            objs.append({"name": nm, "material": "pole", "verts": pv, "faces": pf})
        elif t == "HYDRANT":
            hv, hf = mesh_cylinder(x, y, z0, 0.25, min(H, 1.1), n=10)
            objs.append({"name": nm, "material": "hydrant", "verts": hv, "faces": hf})
        elif t in ("MANHOLE_COVER", "CATCH_BASIN"):
            dv, df = mesh_cylinder(x, y, z0, 0.45, 0.06, n=12)
            objs.append({"name": nm, "material": "metal", "verts": dv, "faces": df})
        elif t in ("TRAFFIC_SIGNAL", "STAND_ALONE_PEDESTRIAN_HEAD", "FLASHING_BEACONS"):
            pv, pf = mesh_cylinder(x, y, z0, 0.10, H)
            objs.append({"name": nm, "material": "pole", "verts": pv, "faces": pf})
            hv, hf = mesh_box((x, y, z0 + H), (0.4, 0.4, 0.9))
            objs.append({"name": nm + "_head", "material": "signal", "verts": hv, "faces": hf})
        else:
            bv, bf = mesh_box((x, y, z0 + H / 2), (0.5, 0.5, H))
            objs.append({"name": nm, "material": "misc", "verts": bv, "faces": bf})
        if len(objs) > start:       # base object carries the relocation anchor
            objs[start]["anchor"] = [x, y, z0]
    return objs


# ---------- writer ----------

def write_obj(path, objects, mtl_filename=None):
    lines = []
    if mtl_filename:
        lines.append(f"mtllib {mtl_filename}")
    voffset = 0
    for o in objects:
        lines.append(f"o {o['name']}")
        if o.get("material"):
            lines.append(f"usemtl {o['material']}")
        for x, y, z in o["verts"]:
            lines.append(f"v {x:.4f} {y:.4f} {z:.4f}")
        for f in o["faces"]:
            lines.append("f " + " ".join(str(i + 1 + voffset) for i in f))
        voffset += len(o["verts"])
    open(path, "w").write("\n".join(lines) + "\n")


def write_dummy_scene(path="out/scene.obj"):
    """Two car shapes so the APS round-trip can be tested before segmentation exists."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cars = [{"center": (0, 0, 0.75), "length": 4.5, "width": 1.8, "height": 1.5, "yaw": 0},
            {"center": (8, 0, 0.75), "length": 4.5, "width": 1.8, "height": 1.5, "yaw": 0}]
    write_obj(path, car_objects(cars))
