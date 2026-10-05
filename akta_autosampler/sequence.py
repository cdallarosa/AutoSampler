"""
Gantry-only sequences and the threaded sequence runner.

A sequence file (config/sequences/*.json) looks like::

    {"name": "...", "steps": [{"type": "home"}, {"type": "move_to_well", ...}, ...]}

Step types: home, move_to_well, lower, raise, dwell, pause, move_to_position,
wait_for_akta, signal_akta, and the "samples" macro, which expands to the
per-vial move/lower/wait/raise(/wash) steps, optionally with the ÄKTA
handshake from config/akta.json.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple, Type, Union

from .akta import AktaLink, describe_condition
from .deck import Deck
from .gantry import Gantry

logger = logging.getLogger(__name__)


class StepError(RuntimeError):
    pass


class Aborted(Exception):
    """STOP: halt where we are."""


class Interrupted(Exception):
    """End: finish gracefully (needle out of the bottle, then park)."""


def where(slot: str, well: str) -> str:
    """Operator wording for a position: 'A1', 'Sample position', or 'rack:B2' for other slots."""
    if slot == "sample":
        return "Sample position"
    return well if slot == "bottles" else f"{slot}:{well}"


@dataclass
class StepContext:
    gantry: Gantry
    deck: Deck
    runner: "SequenceRunner"
    akta: Optional[AktaLink] = None
    current_well: Optional[Tuple[str, str]] = None  # (slot, well) the needle is over

    def require_akta(self) -> AktaLink:
        if self.akta is None:
            raise StepError("This step needs the ÄKTA link (config/akta.json mode is 'off')")
        return self.akta


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
    label: str = ""  # e.g. "Sample 2 'Lot 42'" or "Wash", for the outline and log
    height: str = "top"  # "top" = needle down to the bottle top, "safe" = stay at the safe Z height
    type: ClassVar[str] = "move_to_well"

    def describe(self):
        text = f"Move to {where(self.slot, self.well)}" + (f" - {self.label}" if self.label else "")
        return text + (" (needle stays up)" if self.height == "safe" else "")

    def run(self, ctx):
        if self.height not in ("top", "safe"):
            raise ValueError(f"move_to_well height must be 'top' or 'safe', not {self.height!r}")
        x, y, z = ctx.deck.well_xyz(self.slot, self.well, "top")
        if self.height == "safe":
            z = ctx.gantry.config.z_safe_mm
        _require(ctx.gantry.safe_move_to(x, y, z), f"Move to {where(self.slot, self.well)} failed")
        ctx.current_well = (self.slot, self.well)


@_register
@dataclass
class LowerStep(Step):
    """Lower into the current well: 'sample', 'top', or a number of mm below the top."""
    depth: Union[str, float] = "sample"
    type: ClassVar[str] = "lower"

    def describe(self):
        if isinstance(self.depth, (int, float)):
            return f"Lower needle {self.depth:g} mm below the bottle top"
        return "Lower needle to sampling depth" if self.depth == "sample" else "Lower needle to the bottle top"

    def run(self, ctx):
        _require(ctx.current_well is not None, "Lower needs a preceding move to a bottle")
        slot, well = ctx.current_well
        if isinstance(self.depth, (int, float)):
            z = ctx.deck.well_xyz(slot, well, "top")[2] + float(self.depth)
        else:
            z = ctx.deck.well_xyz(slot, well, self.depth)[2]
        _require(ctx.gantry.move_to(z=z, wait=True), f"Lower in {where(slot, well)} failed")


@_register
@dataclass
class RaiseStep(Step):
    type: ClassVar[str] = "raise"

    def describe(self):
        return "Raise needle to safe height"

    def run(self, ctx):
        _require(ctx.gantry.raise_z(), "Raise failed")


@_register
@dataclass
class DwellStep(Step):
    seconds: float
    type: ClassVar[str] = "dwell"

    def describe(self):
        return f"Wait {self.seconds:g} s"

    def run(self, ctx):
        ctx.runner.sleep(self.seconds)


@_register
@dataclass
class PauseStep(Step):
    """Wait for the operator (or, later, an ÄKTA I/O trigger) to resume."""
    message: str = "Paused - click Resume to continue"
    type: ClassVar[str] = "pause"

    def describe(self):
        return f"Pause for operator: {self.message}"

    def run(self, ctx):
        ctx.runner.wait_for_operator(self.message)


@_register
@dataclass
class MoveToPositionStep(Step):
    name: str
    type: ClassVar[str] = "move_to_position"

    def describe(self):
        return f"Move to {self.name}"

    def run(self, ctx):
        x, y, z = ctx.deck.position(self.name)
        _require(ctx.gantry.safe_move_to(x, y, z), f"Move to '{self.name}' failed")
        ctx.current_well = None


_CONDITION_KEYS = ("equals", "not_equals", "in", "contains")


@_register
@dataclass
class WaitForAktaStep(Step):
    """Wait until an ÄKTA signal matches, e.g. {"signal": "sample_request", "equals": 0}."""
    signal: str
    condition: Dict[str, Any]
    timeout_s: Optional[float] = None
    message: str = ""
    type: ClassVar[str] = "wait_for_akta"

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "WaitForAktaStep":
        cond = {k: item.pop(k) for k in _CONDITION_KEYS if k in item}
        if len(cond) != 1:
            raise ValueError(f"wait_for_akta needs exactly one of {_CONDITION_KEYS}")
        return cls(condition=cond, **item)

    def describe(self):
        return self.message or f"Wait for ÄKTA {self.signal} {describe_condition(self.condition)}"

    def run(self, ctx):
        akta = ctx.require_akta()
        ctx.runner.message = self.describe()
        ok = akta.wait_for(self.signal, self.condition, self.timeout_s, cancel=ctx.runner.interrupt_event)
        ctx.runner.message = ""
        ctx.runner.check_interrupt()
        _require(ok, f"Timed out after {self.timeout_s} s: {self.describe()}")


@_register
@dataclass
class SignalAktaStep(Step):
    """Set an ÄKTA output signal, optionally as a pulse back to its idle value."""
    signal: str
    value: Any
    pulse_s: Optional[float] = None
    label: str = ""  # operator wording, e.g. "Tell ÄKTA: needle is in the bottle"
    type: ClassVar[str] = "signal_akta"

    def describe(self):
        if self.label:
            return self.label
        pulse = f" (pulse {self.pulse_s:g} s)" if self.pulse_s else ""
        return f"Set ÄKTA {self.signal} = {self.value}{pulse}"

    def run(self, ctx):
        akta = ctx.require_akta()
        if self.pulse_s:
            akta.pulse(self.signal, self.value, self.pulse_s, cancel=ctx.runner.interrupt_event)
        else:
            akta.write(self.signal, self.value)


def _expand_samples(spec: Dict[str, Any], deck: Optional[Deck],
                    handshake: Optional[Dict[str, Any]] = None) -> List[Step]:
    """
    "samples" macro::

        {"type": "samples", "slot": "bottles", "wells": "A1-A4" | ["A1", ...],
         "depth": "sample", "dwell_s": 30, "pause_in_sample": false,
         "akta_handshake": false,
         "wash": {"position": "wash", "dwell_s": 5}, "end_position": "park"}

    With ``akta_handshake`` each vial runs: move over vial -> wait for the
    ÄKTA request -> lower -> needle_ready active -> wait for ÄKTA done ->
    needle_ready idle -> raise.
    """
    slot = spec["slot"]
    wells = spec["wells"]
    if isinstance(wells, str):
        if deck is None:
            raise ValueError("Well ranges need a deck to expand")
        wells = deck.slot(slot).labware.expand_wells(wells)
    use_hs = spec.get("akta_handshake", False)
    if use_hs and not handshake:
        raise ValueError("akta_handshake needs a 'handshake' section in config/akta.json")
    hs = handshake or {}
    timeout = hs.get("timeout_s")

    def wait(key: str, msg: str) -> WaitForAktaStep:
        cond = {k: v for k, v in hs[key].items() if k in _CONDITION_KEYS}
        return WaitForAktaStep(hs[key]["signal"], cond, timeout, msg)

    steps: List[Step] = []
    for well in wells:
        steps.append(MoveToWellStep(slot, well, spec.get("label", "")))
        if use_hs:
            steps.append(wait("request", f"Wait for ÄKTA to request the sample ({where(slot, well)})"))
        steps.append(LowerStep(spec.get("depth", "sample")))
        if use_hs:
            # The ÄKTA sets the pace: signal ready at once; any dwell is extra time in the
            # bottle after the ÄKTA reports done (e.g. to let the line settle) before raising.
            ready = hs["ready"]
            steps += [SignalAktaStep(ready["signal"], ready["active"], label="Tell ÄKTA: needle is in the bottle"),
                      wait("done", f"Wait for ÄKTA to finish loading ({where(slot, well)})")]
            if spec.get("dwell_s"):
                steps.append(DwellStep(spec["dwell_s"]))
            steps.append(SignalAktaStep(ready["signal"], ready["idle"], label="Tell ÄKTA: needle is leaving"))
        elif spec.get("dwell_s"):
            steps.append(DwellStep(spec["dwell_s"]))
        if spec.get("pause_in_sample"):
            steps.append(PauseStep(f"Needle in {where(slot, well)}. Run the ÄKTA sample "
                                   f"application, then Continue."))
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


def parse_steps(items: List[Dict[str, Any]], deck: Optional[Deck] = None,
                handshake: Optional[Dict[str, Any]] = None) -> List[Step]:
    steps: List[Step] = []
    for i, item in enumerate(items):
        item = {k: v for k, v in item.items() if not k.startswith("_")}
        kind = item.pop("type", None)
        try:
            if kind == "samples":
                steps += _expand_samples(item, deck, handshake)
            elif kind in _STEP_TYPES:
                cls = _STEP_TYPES[kind]
                steps.append(cls.from_item(item) if hasattr(cls, "from_item") else cls(**item))
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
    def from_dict(cls, data: Dict[str, Any], deck: Optional[Deck] = None,
                  handshake: Optional[Dict[str, Any]] = None) -> "Sequence":
        items = data.get("steps", [])
        return cls(data.get("name", "Unnamed"), items, parse_steps(items, deck, handshake))

    @classmethod
    def load(cls, path: Path, deck: Optional[Deck] = None,
             handshake: Optional[Dict[str, Any]] = None) -> "Sequence":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f), deck, handshake)

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
    ENDED = "ended"      # End: finished the current bottle and parked
    ABORTED = "stopped"  # STOP: halted in place
    FAILED = "failed"


class SequenceRunner:
    """
    Runs a Sequence in a background thread.

    - pause():  pause after the current step (pause_pending is True until then)
    - resume(): continue after a pause
    - end():    graceful - finish the bottle the needle is in (up to its raise),
                then go to the end position / park. If the needle is not in a
                bottle, any wait is interrupted and it parks straight away.
    - abort():  STOP - halt all motion where it is. Nothing moves afterwards
                until the operator says so; ÄKTA outputs return to idle.
    """

    def __init__(self, gantry: Gantry, deck: Deck, akta: Optional[AktaLink] = None):
        self.gantry = gantry
        self.deck = deck
        self.akta = akta
        self.state = RunnerState.IDLE
        self.sequence: Optional[Sequence] = None
        self.index = -1
        self.message = ""
        self.needle_down = False
        self.current_well: Optional[Tuple[str, str]] = None
        self.step_started: Optional[float] = None
        self.stopped_index: Optional[int] = None  # step index where STOP / failure happened
        self._thread: Optional[Thread] = None
        self._abort = Event()
        self._interrupt = Event()  # set by STOP, and by End when the needle is out
        self._end_requested = Event()
        self._resume = Event()
        self._pause_requested = Event()
        self.on_change: Optional[Callable[[], None]] = None

    @property
    def is_active(self) -> bool:
        return self.state in (RunnerState.RUNNING, RunnerState.PAUSED)

    @property
    def abort_event(self) -> Event:
        return self._abort

    @property
    def interrupt_event(self) -> Event:
        return self._interrupt

    @property
    def pause_pending(self) -> bool:
        return self.state == RunnerState.RUNNING and self._pause_requested.is_set()

    @property
    def end_pending(self) -> bool:
        return self.is_active and self._end_requested.is_set()

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
        self.needle_down = False
        self.current_well = None
        self.stopped_index = None
        for ev in (self._abort, self._interrupt, self._end_requested, self._resume, self._pause_requested):
            ev.clear()
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

    def end(self):
        """Graceful end: finish the current bottle, raise, park."""
        if not self.is_active:
            return
        self._end_requested.set()
        self._pause_requested.clear()
        if not self.needle_down:
            self._interrupt.set()
        self._resume.set()
        logger.info("End requested - " + ("finishing the current bottle, then parking"
                                         if self.needle_down else "parking"))

    def abort(self):
        """STOP: halt immediately and stay there."""
        if not self.is_active:
            return
        logger.warning("STOP - run halted")
        self._abort.set()
        self._interrupt.set()
        self._resume.set()
        self.gantry.stop()

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    # Called from steps (runner thread)

    def check_interrupt(self):
        if self._abort.is_set():
            raise Aborted()
        if self._interrupt.is_set():
            raise Interrupted()

    def sleep(self, seconds: float):
        if self._interrupt.wait(seconds):
            self.check_interrupt()

    def wait_for_operator(self, message: str):
        self._resume.clear()
        self.message = message
        self._set_state(RunnerState.PAUSED)
        logger.info(f"Waiting for operator: {message}")
        self._resume.wait()
        self.check_interrupt()
        self.message = ""
        self._set_state(RunnerState.RUNNING)

    def _set_state(self, state: RunnerState):
        self.state = state
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _check_flow(self):
        if self._abort.is_set():
            raise Aborted()
        if self._end_requested.is_set() and not self.needle_down:
            raise Interrupted()

    def _after_step(self, step: Step, ctx: StepContext):
        if step.type == "lower":
            self.needle_down = True
        elif step.type in ("raise", "move_to_well", "move_to_position", "home"):
            self.needle_down = False
        self.current_well = ctx.current_well

    def _run(self):
        ctx = StepContext(self.gantry, self.deck, self, self.akta)
        name = self.sequence.name
        logger.info(f"Run '{name}' started ({len(self.steps)} steps)")
        started = time.time()
        try:
            for i, step in enumerate(self.steps):
                self._check_flow()
                if self._pause_requested.is_set():
                    self.wait_for_operator("Paused by operator")
                self.index = i
                self.step_started = time.time()
                logger.info(f"[{i + 1}/{len(self.steps)}] {step.describe()}")
                step.run(ctx)
                self._after_step(step, ctx)
            self._check_flow()
            self.index = len(self.steps)
            self._set_state(RunnerState.COMPLETED)
            logger.info(f"Run '{name}' completed in {time.time() - started:.1f} s")
        except Interrupted:
            self._reset_akta()
            self.message = "Ended by operator"
            logger.info(f"Run '{name}' ended by operator at step {self.index + 1}")
            self._park()
            self._set_state(RunnerState.ENDED)
        except Exception as e:
            self.stopped_index = max(self.index, 0)
            if isinstance(e, Aborted) or self._abort.is_set():
                self.message = f"Stopped at step {self.index + 1}"
                logger.warning(f"Run '{name}' stopped at step {self.index + 1} "
                               f"(needle {'down' if self.needle_down else 'up'}) - nothing will move "
                               "until the operator chooses")
                self._reset_akta()
                self._set_state(RunnerState.ABORTED)
            else:
                self.message = str(e)
                logger.error(f"Run '{name}' failed at step {self.index + 1}: {e}")
                self.gantry.stop()
                self._reset_akta()
                self._set_state(RunnerState.FAILED)

    def _reset_akta(self):
        """Return ÄKTA outputs (e.g. needle_ready) to idle so the ÄKTA doesn't load air."""
        if self.akta is not None:
            self.akta.reset_outputs()

    def _park(self):
        """End: needle out of the bottle, then the method's end position (or park)."""
        g = self.gantry
        if not g.is_homed or any(a.is_faulted for a in g.axes.values()):
            return
        if self.needle_down:
            g.raise_z()
            self.needle_down = False
        last = self.steps[-1] if self.steps else None
        if isinstance(last, MoveToPositionStep):
            name = last.name
        elif "park" in self.deck.positions:
            name = "park"
        else:
            return
        logger.info(f"Moving to {name}")
        g.safe_move_to(*self.deck.position(name))
        self.current_well = None


def resume_from(sequence: Sequence, step_index: int) -> Sequence:
    """A copy of ``sequence`` that restarts at the bottle containing ``step_index``."""
    start = 0
    for i in range(min(step_index, len(sequence.steps) - 1), -1, -1):
        if sequence.steps[i].type == "move_to_well":
            start = i
            break
    return Sequence(f"{sequence.name} (from step {start + 1})", sequence.items, sequence.steps[start:])
