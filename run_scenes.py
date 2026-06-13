"""Run the full pipeline for multiple scene blocks and register them in scenes.json.

Each entry picks a different 80×80 m ROI from the available LAZ tiles, produces
out/<block_id>/ artifacts, uploads/translates via APS, and writes the URN into
viewer/scenes.json so the viewer dropdown picks it up immediately.

Usage:
    python run_scenes.py                  # run all scenes
    python run_scenes.py elm_st college   # run specific block ids

Add new blocks by appending to SCENES below.
"""
import os
import subprocess
import sys

LAZ_DIR = os.environ.get("LAZ_DIR", "./laz")

# Each block: id, human label, ROI center in UTM 32619, and the tile whose
# centroid is closest (so SMRF has the densest coverage of that block).
# Tile filename prefixes let us pick the right LAZ file via LAZ_DIR override.
SCENES = [
    {
        "id": "davis_sq_a",
        "label": "Davis Square A",
        "roi_cx": 325373.82,
        "roi_cy": 4696009.13,
        # tile 80_10000 centroid closest; fall back to glob order if not found
    },
    {
        "id": "elm_st",
        "label": "Elm St / Dover St",
        "roi_cx": 325148.0,
        "roi_cy": 4696311.0,
        # tile 78_8000 centroid
    },
    {
        "id": "highland_ave",
        "label": "Highland Ave",
        "roi_cx": 325587.0,
        "roi_cy": 4695831.0,
        # tile 46_4000 centroid
    },
    {
        "id": "college_ave",
        "label": "College Ave / Cameron",
        "roi_cx": 325625.0,
        "roi_cy": 4696320.0,
        # tile 80_12000 centroid
    },
]


def run_scene(s):
    env = {
        **os.environ,
        "LAZ_DIR": LAZ_DIR,
        "BLOCK_ID": s["id"],
        "SCENE_LABEL": s["label"],
        "ROI_CX": str(s["roi_cx"]),
        "ROI_CY": str(s["roi_cy"]),
        "ROI_M": "80",
    }
    print(f"\n{'='*60}")
    print(f"  Running scene: {s['id']} — {s['label']}")
    print(f"  ROI center: ({s['roi_cx']:.1f}, {s['roi_cy']:.1f})")
    print(f"{'='*60}\n", flush=True)
    result = subprocess.run(
        ["python", "run.py"],
        env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    if result.returncode != 0:
        print(f"  !! Scene '{s['id']}' failed (exit {result.returncode})", flush=True)
    return result.returncode


if __name__ == "__main__":
    targets = sys.argv[1:]
    to_run = [s for s in SCENES if not targets or s["id"] in targets]
    if not to_run:
        print("No matching scenes. Available:", [s["id"] for s in SCENES])
        sys.exit(1)

    failures = []
    for s in to_run:
        code = run_scene(s)
        if code != 0:
            failures.append(s["id"])

    print(f"\n{'='*60}")
    if failures:
        print(f"Finished with failures: {failures}")
        sys.exit(1)
    else:
        print(f"All {len(to_run)} scene(s) completed successfully.")
