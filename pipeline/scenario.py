"""Scenario — the JSON source of truth for one proposal.

The viewer's fragment transforms and recolors are ephemeral; this document
persists them. The cost engine consumes `scenario.edits` (see pipeline.cost).
Shape (per the implementation guide):

    {"block_id": "davis_sq_a", "crs": "EPSG:26919", "edits": [ {op, ...}, ... ]}

All edit coordinates are in the scenario's CRS (the block's working CRS, UTM),
which is the source of truth; the viewer converts its local model coords to/from
this using the scene origin (see out/scene_meta.json).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CRS = "EPSG:26919"


@dataclass
class Scenario:
    block_id: str = "block"
    crs: str = DEFAULT_CRS
    edits: list[dict] = field(default_factory=list)

    # -- mutation --------------------------------------------------------

    def add_edit(self, edit: dict) -> dict:
        """Append one edit (last-wins for repeated relocates of one target)."""
        self.edits.append(dict(edit))
        return self.edits[-1]

    def undo(self) -> dict | None:
        """Pop and return the most recent edit, or None if empty."""
        return self.edits.pop() if self.edits else None

    def clear(self) -> None:
        self.edits = []

    def edits_for(self, target: str) -> list[dict]:
        return [e for e in self.edits if e.get("target") == target]

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        return {"block_id": self.block_id, "crs": self.crs, "edits": self.edits}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        return cls(block_id=d.get("block_id", "block"),
                   crs=d.get("crs", DEFAULT_CRS),
                   edits=list(d.get("edits", [])))

    @classmethod
    def load(cls, path) -> "Scenario":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def load_or_new(cls, path, **defaults) -> "Scenario":
        p = Path(path)
        if p.exists():
            return cls.load(p)
        return cls(**defaults)

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())


# ---------------------------------------------------------------- edit builders

def relocate_edit(target: str, from_utm, to_utm, asset_type: str | None = None) -> dict:
    """A relocate edit; move distance is derivable from from_utm/to_utm."""
    edit = {"op": "relocate", "target": target,
            "from_utm": [float(v) for v in from_utm],
            "to_utm": [float(v) for v in to_utm]}
    if asset_type:
        edit["asset_type"] = asset_type
    return edit


def add_ramp_edit(at_utm) -> dict:
    return {"op": "add_ramp", "at_utm": [float(v) for v in at_utm]}


def add_road_edit(target: str, path_utm, width_m: float,
                  length_m: float | None = None) -> dict:
    """A new road segment: a polyline of [x, y, z] points and a width (metres).

    `length_m` (horizontal run) is carried so the cost engine stays geometry-free;
    if omitted the cost engine recomputes it from `path_utm`.
    """
    edit = {"op": "add_road", "target": target, "asset_type": "road",
            "path_utm": [[float(v) for v in pt] for pt in path_utm],
            "width_m": float(width_m)}
    if length_m is not None:
        edit["length_m"] = float(length_m)
    return edit
