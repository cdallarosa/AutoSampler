"""
Gantry-only sequences and the threaded sequence runner.

A sequence file (config/sequences/*.json) looks like::

    {"name": "...", "steps": [{"type": "home"}, {"type": "move_to_well", ...}, ...]}

Step types: home, move_to_well, lower, raise, dwell, pause, move_to_position,
and the "samples" macro, which expands to the per-vial
move/lower/wait/raise(/wash) steps.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple, Type, Union

from .deck import Deck
from .gantry import Gantry

logger = logging.getLogger(__name__)


class StepError(RuntimeError):
    pass


class Aborted(Exception):
    pass


@dataclass
class StepContext:
    gantry: Gantry
    deck: Deck
    runner: "SequenceRunner"
    current_well: Optional[Tuple[str, str]] = None  # (slot, well) the needle is over


def _require(ok: bool, message: str):
    if not ok:
        raise StepError(message)


# ============================================================================
# STEPS
# ============================================================================

_STEP_TYPES: Dict[str, Type["Step"]] = {}


def _register(cls):
    _STEP_TYPES[cls.type] = cls
    return cls


class Step:
    type: ClassVar[str] = ""

    def describe(self) -> str:
        raise NotImplementedError

    def run(self, ctx: StepContext) -> None:
        raise NotImplementedError


@_register
@dataclass
class HomeStep(Step):
    type: ClassVar[str] = "home"

    def describe(self):
        return "Home all axes"

    def run(self, ctx):
        _require(ctx.gantry.home_all(), "Homing failed")
        ctx.current_well = None


@_register
@dataclass
class MoveToWellStep(Step):
    slot: str
    well: str
    type: ClassVar[str] = "move_to_well"

    def describe(self):
        return f"Move to {self.slot}:{self.well}"

    def run(self, ctx):
        x, y, z = ctx.deck.well_xyz(self.slot, self.well, "top")
        _require(ctx.gantry.safe_move_to(x, y, z), f"Move to {self.slot}:{self.well} failed")
        ctx.current_well = (self.slot, self.well)


@_register
@dataclass
class LowerStep(Step):
    """Lower into the current well: 'sample', 'top', or a number of mm below the top."""
    depth: Union[str, float] = "sample"
    type: ClassVar[str] = "lower"

    def describe(self):
        return f"Lower to {self.depth}" + (" mm" if isinstance(self.depth, (int, float)) else "")

    def run(self, ctx):
        _require(ctx.current_well is not None, "Lower needs a preceding move_to_well")
        slot, well = ctx.current_well
        if isinstance(self.depth, (int, float)):
            z = ctx.deck.well_xyz(slot, well, "top")[2] + float(self.depth)
        else:
            z = ctx.deck.well_xyz(slot, well, self.depth)[2]
        _require(ctx.gantry.move_to(z=z, wait=True), f"Lower in {slot}:{well} failed")


@_register
@dataclass
class RaiseStep(Step):
    type: ClassVar[str] = "raise"

    def describe(self):
        return "Raise to safe height"

    def run(self, ctx):
        _require(ctx.gantry.raise_z(), "Raise failed")


@_register
@dataclass
class DwellStep(Step):
    seconds: float
    type: ClassVar[str] = "dwell"

    def describe(self):
        return f"Dwell {self.seconds:g} s"

    def run(self, ctx):
        ctx.runner.sleep(self.seconds)


@_register
@dataclass
class PauseStep(Step):
    """Wait for the operator (or, later, an ÄKTA I/O trigger) to resume."""
    message: str = "Paused - click Resume to continue"
    type: ClassVar[str] = "pause"

    def describe(self):
        return f"Pause: {self.message}"

    def run(self, ctx):
        ctx.runner.wait_for_operator(self.message)


@_register
@dataclass
class MoveToPositionStep(Step):
    name: str
    type: ClassVar[str] = "move_to_position"

    def describe(self):
        return f"Move to position '{self.name}'"

    def run(self, ctx):
        x, y, z = ctx.deck.position(self.name)
        _require(ctx.gantry.safe_move_to(x, y, z), f"Move to '{self.name}' failed")
        ctx.current_well = None


def _expand_samples(spec: Dict[str, Any], deck: Optional[Deck]) -> List[Step]:
    """
    "samples" macro::

        {"type": "samples", "slot": "rack1", "wells": "A1-A4" | ["A1", ...],
         "depth": "sample", "dwell_s": 30, "pause_in_sample": false,
         "wash": {"position": "wash", "dwell_s": 5}, "end_position": "park"}
    """
    slot = spec["slot"]
    wells = spec["wells"]
    if isinstance(wells, str):
        if deck is None:
            raise ValueError("Well ranges need a deck to expand")
        wells = deck.slot(slot).labware.expand_wells(wells)
    steps: List[Step] = []
    for well in wells:
        steps += [MoveToWellStep(slot, well), LowerStep(spec.get("depth", "sample"))]
        if spec.get("pause_in_sample"):
            steps.append(PauseStep(f"Needle in {slot}:{well}. Run the ÄKTA sample "
                                   f"application, then Resume."))
        if spec.get("dwell_s"):
            steps.append(DwellStep(spec["dwell_s"]))
        steps.append(RaiseStep())
        wash = spec.get("wash")
        if wash:
            steps.append(MoveToPositionStep(wash["position"]))
            if wash.get("dwell_s"):
                steps.append(DwellStep(wash["dwell_s"]))
            steps.append(RaiseStep())
    if spec.get("end_position"):
        steps.append(MoveToPositionStep(spec["end_position"]))
    return steps


def parse_steps(items: List[Dict[str, Any]], deck: Optional[Deck] = None) -> List[Step]:
    steps: List[Step] = []
    for i, item in enumerate(items):
        item = {k: v for k, v in item.items() if not k.startswith("_")}
        kind = item.pop("type", None)
        try:
            if kind == "samples":
                steps += _expand_samples(item, deck)
            elif kind in _STEP_TYPES:
                steps.append(_STEP_TYPES[kind](**item))
            else:
                raise ValueError(f"unknown type '{kind}'. Types: {sorted(_STEP_TYPES) + ['samples']}")
        except (TypeError, KeyError, ValueError) as e:
            raise ValueError(f"Step {i + 1}: {e}") from None
    return steps


@dataclass
class Sequence:
    name: str
    items: List[Dict[str, Any]]  # as written in the file
    steps: List[Step] = field(default_factory=list)  # expanded

    @classmethod
    def from_dict(cls, data: Dict[str, Any], deck: Optional[Deck] = None) -> "Sequence":
        items = data.get("steps", [])
        return cls(data.get("name", "Unnamed"), items, parse_steps(items, deck))

    @classmethod
    def load(cls, path: Path, deck: Optional[Deck] = None) -> "Sequence":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f), deck)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "steps": self.items}

    def save(self, path: Path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            f.write("\n")


# ============================================================================
# RUNNER
# ============================================================================

class RunnerState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class SequenceRunner:
    """Runs a Sequence in a background thread with pause/resume/abort."""

    def __init__(self, gantry: Gantry, deck: Deck):
        self.gantry = gantry
        self.deck = deck
        self.state = RunnerState.IDLE
        self.sequence: Optional[Sequence] = None
        self.index = -1
        self.message = ""
        self._thread: Optional[Thread] = None
        self._abort = Event()
        self._resume = Event()
        self._pause_requested = Event()
        self.on_change: Optional[Callable[[], None]] = None

    @property
    def is_active(self) -> bool:
        return self.state in (RunnerState.RUNNING, RunnerState.PAUSED)

    @property
    def steps(self) -> List[Step]:
        return self.sequence.steps if self.sequence else []

    def start(self, sequence: Sequence) -> bool:
        if self.is_active:
            logger.error("A sequence is already running")
            return False
        self.sequence = sequence
        self.index = -1
        self.message = ""
        self._abort.clear()
        self._resume.clear()
        self._pause_requested.clear()
        self._set_state(RunnerState.RUNNING)
        self._thread = Thread(target=self._run, daemon=True, name="sequence-runner")
        self._thread.start()
        return True

    def pause(self):
        """Pause after the current step finishes."""
        if self.state == RunnerState.RUNNING:
            self._pause_requested.set()
            logger.info("Pause requested - will pause after the current step")

    def resume(self):
        self._pause_requested.clear()
        self._resume.set()

    def abort(self):
        """Stop motion immediately; the runner then raises Z to safe height."""
        if not self.is_active:
            return
        logger.warning("Sequence abort requested")
        self._abort.set()
        self._resume.set()
        self.gantry.stop()

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    # Called from steps (runner thread)

    def sleep(self, seconds: float):
        if self._abort.wait(seconds):
            raise Aborted()

    def wait_for_operator(self, message: str):
        self._resume.clear()
        self.message = message
        self._set_state(RunnerState.PAUSED)
        logger.info(f"Waiting for operator: {message}")
        self._resume.wait()
        if self._abort.is_set():
            raise Aborted()
        self.message = ""
        self._set_state(RunnerState.RUNNING)

    def _set_state(self, state: RunnerState):
        self.state = state
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _run(self):
        ctx = StepContext(self.gantry, self.deck, self)
        name = self.sequence.name
        logger.info(f"Sequence '{name}' started ({len(self.steps)} steps)")
        started = time.time()
        try:
            for i, step in enumerate(self.steps):
                if self._abort.is_set():
                    raise Aborted()
                if self._pause_requested.is_set():
                    self.wait_for_operator("Paused by user")
                self.index = i
                logger.info(f"[{i + 1}/{len(self.steps)}] {step.describe()}")
                step.run(ctx)
            self.index = len(self.steps)
            self._set_state(RunnerState.COMPLETED)
            logger.info(f"Sequence '{name}' completed in {time.time() - started:.1f} s")
        except Exception as e:
            if isinstance(e, Aborted) or self._abort.is_set():
                self.message = "Aborted"
                logger.warning(f"Sequence '{name}' aborted at step {self.index + 1}")
                self._safe_raise()
                self._set_state(RunnerState.ABORTED)
            else:
                self.message = str(e)
                logger.error(f"Sequence '{name}' failed at step {self.index + 1}: {e}")
                self.gantry.stop()
                self._set_state(RunnerState.FAILED)

    def _safe_raise(self):
        """After an abort, lift the needle out of the vial if the gantry is healthy."""
        g = self.gantry
        if g.is_homed and not any(a.is_faulted for a in g.axes.values()):
            logger.info("Raising Z to safe height after abort")
            g.raise_z()
