"""
Sample manager: what is in each bottle, and the order to visit them.

- BottleRegistry (config/samples.json): name, role and notes per deck position.
  Roles: sample, wash, blank, empty.
- SampleQueue (config/sample_lists/*.json): an ordered list of entries, e.g.
  A1 (sample) -> F5 (wash) -> A2 (sample) -> F5 (wash) ...
  ``to_sequence`` turns it into a runnable Sequence; each entry becomes a
  "samples" macro for one bottle (move, [ÄKTA request], lower, dwell,
  [needle ready / done], raise).
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG_DIR
from .deck import Deck
from .sequence import Sequence

ROLES = ("sample", "wash", "blank", "empty")
KINDS = ("sample", "wash", "blank")
SAMPLE_LIST_DIR = CONFIG_DIR / "sample_lists"


def pos_key(slot: str, well: str) -> str:
    return f"{slot}:{well}"


def split_key(key: str) -> Tuple[str, str]:
    slot, well = key.split(":", 1)
    return slot, well


# ============================================================================
# BOTTLE REGISTRY
# ============================================================================

@dataclass
class BottleInfo:
    slot: str
    well: str
    name: str = ""
    role: str = "sample"
    notes: str = ""

    @property
    def key(self) -> str:
        return pos_key(self.slot, self.well)

    @property
    def display(self) -> str:
        where = "Sample position" if self.slot == "sample" else self.well
        return f"{where} · {self.name}" if self.name else where


class BottleRegistry:
    """Name / role / notes for every position on the deck."""

    def __init__(self, deck: Deck, path: Path = CONFIG_DIR / "samples.json"):
        self.deck = deck
        self.path = path
        self.bottles: Dict[str, BottleInfo] = {}
        for slot in deck.slots.values():
            for well in slot.labware.wells():
                info = BottleInfo(slot.name, well)
                self.bottles[info.key] = info
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, saved in data.get("bottles", {}).items():
                if key in self.bottles:
                    b = self.bottles[key]
                    b.name = saved.get("name", "")
                    b.role = saved.get("role", "sample") if saved.get("role") in ROLES else "sample"
                    b.notes = saved.get("notes", "")

    def get(self, slot: str, well: str) -> BottleInfo:
        try:
            return self.bottles[pos_key(slot, well)]
        except KeyError:
            raise ValueError(f"No position {slot}:{well} on the deck") from None

    def update(self, slot: str, well: str, **changes):
        b = self.get(slot, well)
        if "role" in changes and changes["role"] not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        for k, v in changes.items():
            setattr(b, k, v)

    def with_role(self, role: str) -> List[BottleInfo]:
        return [b for b in self.bottles.values() if b.role == role]

    def sample_wells(self, slot: str, first: str = "A1", order: str = "row") -> List[str]:
        """Bottles of ``slot`` marked 'sample', from ``first`` on, row by row (A1, A2 ...) or column by column
        (A1, B1 ...)."""
        lw = self.deck.slot(slot).labware
        wells = list(lw.wells())
        if order == "column":
            wells.sort(key=lambda w: lw.parse_well(w)[::-1])
        elif order != "row":
            raise ValueError("order must be 'row' or 'column'")
        start = lw.well_name(*lw.parse_well(first))
        wells = wells[wells.index(start):]
        return [w for w in wells if self.get(slot, w).role == "sample"]

    def save(self, path: Optional[Path] = None):
        path = path or self.path
        data = {"bottles": {k: {"name": b.name, "role": b.role, "notes": b.notes}
                            for k, b in self.bottles.items()
                            if b.name or b.notes or b.role != "sample"}}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ============================================================================
# QUEUE
# ============================================================================

@dataclass
class QueueEntry:
    kind: str  # sample | wash | blank
    slot: str
    well: str
    dwell_s: float = 0.0
    depth: str = "sample"
    handshake: bool = False

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, not '{self.kind}'")

    @property
    def key(self) -> str:
        return pos_key(self.slot, self.well)


@dataclass
class SampleQueue:
    name: str = "Sample list"
    entries: List[QueueEntry] = field(default_factory=list)

    # -- editing ---------------------------------------------------------

    def add(self, kind: str, slot: str, well: str, **kw) -> QueueEntry:
        e = QueueEntry(kind, slot, well, **kw)
        self.entries.append(e)
        return e

    def add_sample(self, slot: str, well: str, wash: Optional[Tuple[str, str]] = None, wash_dwell_s: float = 0.0,
                   **kw) -> QueueEntry:
        """Add a sample; with ``wash`` given, a wash goes in first when the previous entry is also a sample
        (so the list reads A1, wash, A2, wash, A3 as you click)."""
        if wash and self.entries and self.entries[-1].kind == "sample":
            self.add("wash", wash[0], wash[1], dwell_s=wash_dwell_s)
        return self.add("sample", slot, well, **kw)

    def move(self, index: int, delta: int):
        j = index + delta
        if 0 <= index < len(self.entries) and 0 <= j < len(self.entries):
            self.entries[index], self.entries[j] = self.entries[j], self.entries[index]

    def remove(self, index: int):
        if 0 <= index < len(self.entries):
            del self.entries[index]

    def insert_washes(self, wash_slot: str, wash_well: str, dwell_s: float = 0.0,
                      handshake: bool = False, after_last: bool = False):
        """Put a wash between every pair of consecutive non-wash entries (A1, wash, A2, wash, A3)."""
        out: List[QueueEntry] = []
        for i, e in enumerate(self.entries):
            out.append(e)
            nxt = self.entries[i + 1] if i + 1 < len(self.entries) else None
            if e.kind != "wash" and ((nxt is not None and nxt.kind != "wash") or (nxt is None and after_last)):
                out.append(QueueEntry("wash", wash_slot, wash_well, dwell_s=dwell_s, handshake=handshake))
        self.entries = out

    @staticmethod
    def build(registry: "BottleRegistry", count: int, slot: str = "bottles", first: str = "A1", order: str = "row",
              sample_dwell_s: float = 0.0, handshake: bool = False,
              wash: Optional[Tuple[str, str]] = None, wash_dwell_s: float = 0.0, wash_after_last: bool = False,
              blank: Optional[Tuple[str, str]] = None, blank_dwell_s: float = 0.0) -> List[QueueEntry]:
        """
        Quick run order: ``count`` sample bottles from ``first`` (row or column order; wash / blank / empty
        bottles are skipped) with an optional wash and/or blank between consecutive samples:
            A1, wash, blank, A2, wash, blank, A3 [, wash]
        The ÄKTA handshake applies to the samples only.
        """
        if count < 1:
            raise ValueError("Number of samples must be at least 1")
        wells = registry.sample_wells(slot, first, order)
        if len(wells) < count:
            raise ValueError(f"Only {len(wells)} sample bottles from {first} on ({order} order) - asked for {count}")
        out: List[QueueEntry] = []
        for i, well in enumerate(wells[:count]):
            out.append(QueueEntry("sample", slot, well, dwell_s=sample_dwell_s, handshake=handshake))
            last = i == count - 1
            if wash and (not last or wash_after_last):
                out.append(QueueEntry("wash", wash[0], wash[1], dwell_s=wash_dwell_s))
            if blank and not last:
                out.append(QueueEntry("blank", blank[0], blank[1], dwell_s=blank_dwell_s))
        return out

    def remove_washes(self):
        self.entries = [e for e in self.entries if e.kind != "wash"]

    # -- conversion ------------------------------------------------------

    def to_items(self, registry: Optional[BottleRegistry] = None, home_first: bool = True,
                 end_position: Optional[str] = "park", pause_in_sample: bool = False) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = [{"type": "home"}] if home_first else []
        n_sample = 0
        for e in self.entries:
            if e.kind == "sample":
                n_sample += 1
            name = registry.get(e.slot, e.well).name if registry else ""
            label = {"sample": f"Sample {n_sample}" + (f" '{name}'" if name else ""),
                     "wash": "Wash" + (f" '{name}'" if name else ""),
                     "blank": "Blank" + (f" '{name}'" if name else "")}[e.kind]
            items.append({"type": "samples", "slot": e.slot, "wells": [e.well], "depth": e.depth,
                          "dwell_s": e.dwell_s, "akta_handshake": e.handshake, "label": label,
                          "pause_in_sample": pause_in_sample and e.kind == "sample"})
        if end_position:
            items.append({"type": "move_to_position", "name": end_position})
        return items

    def to_sequence(self, deck: Deck, handshake: Optional[dict] = None,
                    registry: Optional[BottleRegistry] = None, home_first: bool = True,
                    end_position: Optional[str] = "park", pause_in_sample: bool = False) -> Sequence:
        if not self.entries:
            raise ValueError("The sample list is empty")
        return Sequence.from_dict({"name": self.name,
                                   "steps": self.to_items(registry, home_first, end_position, pause_in_sample)},
                                  deck, handshake)

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "entries": [asdict(e) for e in self.entries]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SampleQueue":
        return cls(data.get("name", "Sample list"), [QueueEntry(**e) for e in data.get("entries", [])])

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SampleQueue":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
