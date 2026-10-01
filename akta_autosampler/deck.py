"""
Deck and labware model.

Labware (config/labware/*.json) is a generic grid: rows x cols wells with a
pitch, the A1 offset from the labware origin, and two Z heights in gantry
coordinates (+Z is down): ``z_top_mm`` (needle tip just above the vessel)
and ``z_sample_mm`` (sampling depth).

The deck (config/deck.json) places labware in named slots by XY origin, and
holds named positions such as park and wash.
"""

import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .config import CONFIG_DIR

_WELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _row_label(index: int) -> str:
    letters = string.ascii_uppercase
    return letters[index] if index < 26 else letters[index // 26 - 1] + letters[index % 26]


def _row_index(label: str) -> int:
    label = label.upper()
    if len(label) == 1:
        return ord(label) - ord("A")
    return (ord(label[0]) - ord("A") + 1) * 26 + ord(label[1]) - ord("A")


@dataclass
class Labware:
    id: str
    name: str
    rows: int
    cols: int
    pitch_x_mm: float
    pitch_y_mm: float
    a1_offset_mm: Tuple[float, float]
    z_top_mm: float
    z_sample_mm: float
    well_diameter_mm: Optional[float] = None

    @classmethod
    def load(cls, path: Path) -> "Labware":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        data["a1_offset_mm"] = tuple(data["a1_offset_mm"])
        return cls(id=path.stem, **data)

    def parse_well(self, well: str) -> Tuple[int, int]:
        """'B3' -> (row 1, col 2). Raises ValueError if not on this labware."""
        m = _WELL_RE.match(well.strip())
        if not m:
            raise ValueError(f"Invalid well name '{well}'")
        row, col = _row_index(m.group(1)), int(m.group(2)) - 1
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise ValueError(f"Well '{well}' not on {self.name} ({self.rows}x{self.cols})")
        return row, col

    def well_name(self, row: int, col: int) -> str:
        return f"{_row_label(row)}{col + 1}"

    def wells(self) -> Iterator[str]:
        """All wells in row-major order (A1, A2, ... B1, ...)."""
        for r in range(self.rows):
            for c in range(self.cols):
                yield self.well_name(r, c)

    def well_offset(self, well: str) -> Tuple[float, float]:
        """XY of a well relative to the labware origin."""
        row, col = self.parse_well(well)
        return (self.a1_offset_mm[0] + col * self.pitch_x_mm,
                self.a1_offset_mm[1] + row * self.pitch_y_mm)

    def expand_wells(self, spec: str) -> List[str]:
        """
        Parse a well list like "A1, A3-A6, B1" (ranges are row-major
        inclusive) into well names.
        """
        all_wells = list(self.wells())
        result = []
        for part in filter(None, (p.strip() for p in spec.replace(";", ",").split(","))):
            if "-" in part:
                start, end = (s.strip() for s in part.split("-", 1))
                i = all_wells.index(self.well_name(*self.parse_well(start)))
                j = all_wells.index(self.well_name(*self.parse_well(end)))
                if i > j:
                    raise ValueError(f"Range '{part}' runs backwards")
                result.extend(all_wells[i:j + 1])
            else:
                result.append(self.well_name(*self.parse_well(part)))
        return result


@dataclass
class Slot:
    name: str
    origin_mm: Tuple[float, float]
    labware: Labware
    z_offset_mm: float = 0.0


@dataclass
class Deck:
    slots: Dict[str, Slot] = field(default_factory=dict)
    positions: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    path: Optional[Path] = None

    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR) -> "Deck":
        path = config_dir / "deck.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        labware_cache: Dict[str, Labware] = {}
        slots = {}
        for name, s in data.get("slots", {}).items():
            lw_id = s["labware"]
            if lw_id not in labware_cache:
                labware_cache[lw_id] = Labware.load(config_dir / "labware" / f"{lw_id}.json")
            slots[name] = Slot(name, tuple(s["origin_mm"]), labware_cache[lw_id], s.get("z_offset_mm", 0.0))
        positions = {k: tuple(v) for k, v in data.get("positions", {}).items()}
        return cls(slots, positions, path)

    def save(self, path: Optional[Path] = None):
        path = path or self.path
        data = {
            "slots": {
                s.name: {"origin_mm": list(s.origin_mm), "labware": s.labware.id,
                         **({"z_offset_mm": s.z_offset_mm} if s.z_offset_mm else {})}
                for s in self.slots.values()
            },
            "positions": {k: list(v) for k, v in self.positions.items()},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    def slot(self, name: str) -> Slot:
        try:
            return self.slots[name]
        except KeyError:
            raise ValueError(f"Unknown slot '{name}'. Slots: {list(self.slots)}") from None

    def well_xy(self, slot: str, well: str) -> Tuple[float, float]:
        s = self.slot(slot)
        dx, dy = s.labware.well_offset(well)
        return s.origin_mm[0] + dx, s.origin_mm[1] + dy

    def well_xyz(self, slot: str, well: str, depth: str = "top") -> Tuple[float, float, float]:
        """Gantry coordinates of a well. depth: 'top' (approach) or 'sample'."""
        s = self.slot(slot)
        x, y = self.well_xy(slot, well)
        if depth == "top":
            z = s.labware.z_top_mm
        elif depth == "sample":
            z = s.labware.z_sample_mm
        else:
            raise ValueError(f"depth must be 'top' or 'sample', not '{depth}'")
        return x, y, z + s.z_offset_mm

    def position(self, name: str) -> Tuple[float, float, float]:
        try:
            return self.positions[name]
        except KeyError:
            raise ValueError(f"Unknown position '{name}'. Positions: {list(self.positions)}") from None

    def teach_a1(self, slot: str, x: float, y: float):
        """Set the slot origin so well A1 is at (x, y)."""
        s = self.slot(slot)
        ax, ay = s.labware.a1_offset_mm
        s.origin_mm = (round(x - ax, 3), round(y - ay, 3))
