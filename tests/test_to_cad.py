from pipeline.to_cad import (
    MATERIALS,
    asset_objects,
    car_objects,
    mesh_box,
    write_mtl,
    write_obj,
)


def test_mesh_box_has_8_vertices_6_faces():
    verts, faces = mesh_box((0, 0, 0.75), (4.5, 1.8, 1.5))
    assert len(verts) == 8
    assert len(faces) == 6


def test_car_objects_named_and_materialed():
    cars = [{"center": (0, 0, 0.75), "length": 4.5, "width": 1.8, "height": 1.5, "yaw": 0}]
    objs = car_objects(cars)
    assert objs[0]["name"] == "car_01"
    assert objs[0]["material"] == "car"
    # body + cabin
    assert any(o["name"] == "car_01_cabin" for o in objs)


def test_write_obj_offsets_face_indices(tmp_path):
    objs = [
        {"name": "a", "material": "asphalt", "verts": [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
         "faces": [(0, 1, 2, 3)]},
        {"name": "b", "material": "concrete", "verts": [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)],
         "faces": [(0, 1, 2, 3)]},
    ]
    p = tmp_path / "scene.obj"
    write_obj(str(p), objs, mtl_filename="scene.mtl")
    body = p.read_text()
    assert "mtllib scene.mtl" in body
    assert body.count("\no ") + body.startswith("o ") >= 2
    # second object's face must reference the global vertex offset (5..8)
    assert "f 5 6 7 8" in body


def test_write_mtl_emits_every_material(tmp_path):
    p = tmp_path / "scene.mtl"
    write_mtl(str(p))
    body = p.read_text()
    for name in MATERIALS:
        assert f"newmtl {name}" in body


def test_asset_objects_skips_ground_covered_types():
    # CURB/SIDEWALK/RAMP are part of the ground mesh, not standalone objects.
    assets = [
        {"type": "CURB", "x": 0, "y": 0, "ground_z": 0, "height": 0.2},
        {"type": "HYDRANT", "x": 1, "y": 1, "ground_z": 0, "height": 1.0},
    ]
    objs = asset_objects(assets)
    names = [o["name"] for o in objs]
    assert any(n.startswith("hydrant_") for n in names)
    assert not any("curb" in n for n in names)
