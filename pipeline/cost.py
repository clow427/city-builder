"""StreetForge cost engine — price a scenario's edits against a unit catalog.

Pure functions, no I/O beyond reading the YAML catalog. The viewer/pipeline
produces `edits` (see the scenario model in the implementation guide) and this
module turns them into an itemized, exportable `CostReport`:

    >>> from pipeline.cost import estimate, load_catalog
    >>> report = estimate(scenario["edits"], load_catalog())
    >>> print(report.to_markdown())

Each edit is a dict with an "op" and op-specific fields. Quantities (areas,
linear feet, asset types) may be carried explicitly on the edit, or resolved
from an optional `objects` map of {object_id: {"area_sqft":..., "length_ft":...,
"asset_type":...}} that the pipeline computes from the mesh. Carrying them on
the edit keeps this module unit-testable with no geometry.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

SQFT_PER_SQM = 10.763910416709722

# repave treatment name -> catalog["pavement"] key
TREATMENT_KEYS = {
    "crack_seal": "crack_seal_sqft",
    "mill_and_overlay": "mill_and_overlay_sqft",
    "full_depth_recon": "full_depth_recon_sqft",
    "full_depth_reconstruct": "full_depth_recon_sqft",
}

_TRAILING_INDEX = re.compile(r"_\d+$")


class CostError(ValueError):
    """A scenario edit could not be priced (bad op, missing quantity, …)."""


# --------------------------------------------------------------------- catalog

def load_catalog(path: str | Path | None = None) -> dict:
    """Load the unit-cost catalog (YAML) into a plain dict.

    Defaults to ``config/unit_costs.yaml`` at the repo root.
    """
    import yaml  # lazy: keeps importing this module cheap

    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "unit_costs.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ data model

@dataclass
class LineItem:
    """One priced row: amount = quantity * unit_cost."""

    op: str
    description: str
    quantity: float
    unit: str
    unit_cost: float
    amount: float
    target: str | None = None

    def rounded(self) -> dict:
        d = asdict(self)
        d["quantity"] = round(self.quantity, 2)
        d["unit_cost"] = round(self.unit_cost, 2)
        d["amount"] = round(self.amount, 2)
        return d


@dataclass
class CostReport:
    """Aggregated estimate: itemized line items + total, exportable."""

    line_items: list[LineItem] = field(default_factory=list)
    currency: str = "USD"

    @property
    def total(self) -> float:
        return round(sum(li.amount for li in self.line_items), 2)

    def by_op(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for li in self.line_items:
            out[li.op] = round(out.get(li.op, 0.0) + li.amount, 2)
        return out

    # -- exports ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "total": self.total,
            "by_op": self.by_op(),
            "line_items": [li.rounded() for li in self.line_items],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["op", "target", "description", "quantity", "unit",
                    "unit_cost", "amount"])
        for li in self.line_items:
            w.writerow([li.op, li.target or "", li.description,
                        round(li.quantity, 2), li.unit,
                        round(li.unit_cost, 2), round(li.amount, 2)])
        w.writerow([])
        w.writerow(["", "", "", "", "", "TOTAL", self.total])
        return buf.getvalue()

    def to_markdown(self) -> str:
        rows = ["| Op | Item | Qty | Unit | $/unit | Amount |",
                "|----|------|----:|------|-------:|-------:|"]
        for li in self.line_items:
            rows.append(
                f"| {li.op} | {li.description} | {li.quantity:,.2f} | {li.unit} "
                f"| {li.unit_cost:,.2f} | ${li.amount:,.2f} |")
        rows.append(f"| | | | | **Total** | **${self.total:,.2f}** |")
        return "\n".join(rows)

    def write(self, path: str | Path) -> None:
        """Write the report, format chosen by extension (.md/.csv/.json)."""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            text = self.to_json()
        elif suffix == ".csv":
            text = self.to_csv()
        elif suffix in (".md", ".markdown"):
            text = self.to_markdown()
        else:
            raise CostError(f"unknown report extension {suffix!r}; use .md/.csv/.json")
        path.write_text(text)


# ------------------------------------------------------------ quantity helpers

def cells_to_sqft(cell_count: float, cell_m: float) -> float:
    """Convert a count of square mesh cells (edge `cell_m`) to square feet."""
    return float(cell_count) * float(cell_m) ** 2 * SQFT_PER_SQM


def _obj_field(edit: dict, objects: dict | None, key: str):
    target = edit.get("target") or edit.get("segment")
    if objects and target in objects and key in objects[target]:
        return objects[target][key]
    return None


def _area_sqft(edit: dict, objects: dict | None) -> float:
    if edit.get("area_sqft") is not None:
        return float(edit["area_sqft"])
    cell_m = edit.get("cell_m")
    if cell_m is not None and edit.get("cells") is not None:
        return cells_to_sqft(len(edit["cells"]), cell_m)
    if cell_m is not None and edit.get("cell_count") is not None:
        return cells_to_sqft(edit["cell_count"], cell_m)
    from_obj = _obj_field(edit, objects, "area_sqft")
    if from_obj is not None:
        return float(from_obj)
    raise CostError(
        f"{edit.get('op')} edit needs area_sqft (or cells+cell_m, or a known "
        f"target with area_sqft); got keys {sorted(edit)}")


def _infer_asset_type(edit: dict, objects: dict | None) -> str:
    if edit.get("asset_type"):
        return str(edit["asset_type"]).lower()
    from_obj = _obj_field(edit, objects, "asset_type")
    if from_obj:
        return str(from_obj).lower()
    target = edit.get("target") or ""
    # "utility_pole_07" -> "utility_pole"
    return _TRAILING_INDEX.sub("", target).lower() or "default"


def _move_distance_m(edit: dict) -> float | None:
    a, b = edit.get("from_utm"), edit.get("to_utm")
    if a is None or b is None:
        return None
    return math.dist(a[:2], b[:2])


# ------------------------------------------------------------------- op pricing

def _price_repave(edit, catalog, objects) -> list[LineItem]:
    treatment = edit.get("treatment", "mill_and_overlay")
    key = TREATMENT_KEYS.get(treatment)
    if key is None:
        raise CostError(f"unknown repave treatment {treatment!r}; "
                        f"expected one of {sorted(TREATMENT_KEYS)}")
    unit_cost = float(catalog["pavement"][key])
    area = _area_sqft(edit, objects)
    return [LineItem("repave", f"{treatment.replace('_', ' ')} pavement",
                     area, "sq ft", unit_cost, area * unit_cost,
                     target=edit.get("target"))]


def _price_relocate(edit, catalog, objects) -> list[LineItem]:
    asset_type = _infer_asset_type(edit, objects)
    table = catalog["relocation"]
    base = float(table.get(asset_type, table["default"]))
    dist = edit.get("distance_m")
    if dist is None:
        dist = _move_distance_m(edit)

    desc = f"relocate {asset_type.replace('_', ' ')}"
    if dist is not None:
        desc += f" ({dist:.1f} m)"
    items = [LineItem("relocate", desc, 1, "each", base, base,
                      target=edit.get("target"))]

    # optional per-meter run cost (e.g. trenching/wiring for utility moves)
    per_m_table = catalog.get("relocation_per_m") or {}
    per_m = float(per_m_table.get(asset_type, per_m_table.get("default", 0.0)))
    if per_m and dist:
        run = dist * per_m
        items.append(LineItem("relocate", f"{asset_type.replace('_', ' ')} run",
                              round(dist, 1), "linear m", per_m, run,
                              target=edit.get("target")))
    return items


def _price_add_ramp(edit, catalog, objects) -> list[LineItem]:
    unit_cost = float(catalog["curb_ramp_each"])
    return [LineItem("add_ramp", "curb ramp", 1, "each", unit_cost, unit_cost,
                     target=edit.get("target"))]


def _price_regrade(edit, catalog, objects) -> list[LineItem]:
    unit_cost = float(catalog["regrade_sqft"])
    area = _area_sqft(edit, objects)
    return [LineItem("regrade", "re-grade surface", area, "sq ft",
                     unit_cost, area * unit_cost, target=edit.get("target"))]


def _price_widen(edit, catalog, objects) -> list[LineItem]:
    """Street widen/narrow: new/removed pavement area + curb. Magnitudes priced.

    Area may come from `area_sqft`, or from `delta_ft` (width change) times the
    segment `length_ft` (explicit or from the objects map).
    """
    items: list[LineItem] = []
    treatment = edit.get("pavement_treatment", "full_depth_recon")
    pave_unit = float(catalog["pavement"][TREATMENT_KEYS[treatment]])

    area = edit.get("area_sqft")
    if area is None and edit.get("delta_ft") is not None:
        length_ft = edit.get("length_ft") or _obj_field(edit, objects, "length_ft")
        if length_ft is not None:
            area = abs(float(edit["delta_ft"])) * float(length_ft)
    if area is not None:
        area = abs(float(area))
        items.append(LineItem("widen", f"pavement ({treatment.replace('_', ' ')})",
                              area, "sq ft", pave_unit, area * pave_unit,
                              target=edit.get("segment")))

    curb_ft = edit.get("curb_ft")
    if curb_ft is None and edit.get("delta_ft") is not None:
        # a width change re-sets one curb line along the segment length
        curb_ft = edit.get("length_ft") or _obj_field(edit, objects, "length_ft")
    if curb_ft:
        curb_ft = abs(float(curb_ft))
        curb_unit = float(catalog["curb_linear_ft"])
        items.append(LineItem("widen", "curb", curb_ft, "linear ft",
                              curb_unit, curb_ft * curb_unit,
                              target=edit.get("segment")))
    if not items:
        raise CostError("widen edit needs area_sqft/curb_ft or delta_ft+length_ft")
    return items


_HANDLERS = {
    "repave": _price_repave,
    "relocate": _price_relocate,
    "add_ramp": _price_add_ramp,
    "regrade": _price_regrade,
    "widen": _price_widen,
    "narrow": _price_widen,
}


# ----------------------------------------------------------------------- public

def estimate(edits: list[dict], catalog: dict, *, objects: dict | None = None,
             strict: bool = True) -> CostReport:
    """Price a list of scenario edits against the unit-cost catalog.

    Args:
        edits: scenario "edits" list; each a dict with an "op" key.
        catalog: parsed unit-cost catalog (see ``load_catalog``).
        objects: optional {object_id: attrs} map for resolving quantities
            (area_sqft / length_ft / asset_type) the edits don't carry.
        strict: when True (default) an unpriceable edit raises CostError; when
            False it is skipped (useful for partial/live previews).

    Returns:
        A CostReport with one or more LineItems per edit.
    """
    report = CostReport()
    for edit in edits:
        op = edit.get("op")
        handler = _HANDLERS.get(op)
        if handler is None:
            if strict:
                raise CostError(f"unknown edit op {op!r}; "
                                f"expected one of {sorted(_HANDLERS)}")
            continue
        try:
            report.line_items.extend(handler(edit, catalog, objects))
        except CostError:
            if strict:
                raise
    return report
