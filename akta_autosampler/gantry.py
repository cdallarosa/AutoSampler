"""
3-axis gantry controller (mm coordinates).

Ported from ProteinMakerV5 system_configuration/odrive/gantry_class.py.
Coordinates are corner-origin: each axis is 0 at its homed end and runs to
``max_mm``. Z=0 is the top (homed) position and +Z moves the needle down.
"""

import dataclasses
import logging
import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import AXIS_NAMES, LOOP_SETTINGS, GantryConfig, save_axis_value
from .tuning import GAIN_LIMITS, analyze_step, describe
from .hardware.axis import AxisConfig, AxisState, AxisType, BaseAxis, MotionProfile

logger = logging.getLogger(__name__)

AxisFactory = Callable[[str, AxisConfig], BaseAxis]

_AXIS_TYPES = {"x": AxisType.X_AXIS, "y": AxisType.Y_AXIS, "z": AxisType.Z_AXIS}


class GantryState(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    HOMING = "homing"
    CALIBRATING = "calibrating"
    MOVING = "moving"
    ERROR = "error"
    STOPPED = "stopped"
    EMERGENCY_STOP = "emergency_stop"


@dataclass
class GantryPosition:
    """3D position in mm"""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __repr__(self):
        return f"Position(X={self.x:.2f}, Y={self.y:.2f}, Z={self.z:.2f})"


def simulated_axis_factory(config: GantryConfig) -> AxisFactory:
    from .hardware.sim_axis import SimulatedAxis

    def make(name: str, axis_config: AxisConfig) -> BaseAxis:
        ax = config.axes[name]
        travel = config.simulation.travel_mm[name] * ax.turns_per_mm
        return SimulatedAxis(axis_config, travel_turns=travel, speedup=config.simulation.speedup)
    return make


def odrive_axis_factory(config: GantryConfig) -> AxisFactory:
    from .hardware.odrive_axis import ODriveAxis, ODriveBoardPool
    from .hardware.odrive_settings import board_settings, motor_settings
    write_cfg = config.motor.write_motor_config
    pool = ODriveBoardPool(board_settings(config.board) if write_cfg else None)

    def make(name: str, axis_config: AxisConfig) -> BaseAxis:
        setup = motor_settings(config.motor, config.axes[name].axis_number) if write_cfg else None
        return ODriveAxis(axis_config, pool, setup)
    return make


class Gantry:
    """Coordinates three axes for XYZ motion in mm."""

    def __init__(self, config: GantryConfig, axis_factory: Optional[AxisFactory] = None):
        self.config = config
        self.state = GantryState.DISCONNECTED
        self.is_homed = False
        self.simulated = config.simulate
        self.speed_factor = min(max(float(config.speed_percent), 1.0), 100.0) / 100  # see set_speed_percent()
        # Axes not wired yet (enabled: false) get a simulated stand-in even in hardware mode
        self.simulated_axes = {n for n in AXIS_NAMES if config.simulate or not config.axes[n].enabled}
        if axis_factory is None:
            sim = simulated_axis_factory(config)
            hw = None if config.simulate else odrive_axis_factory(config)
            axis_factory = lambda n, c: sim(n, c) if n in self.simulated_axes else hw(n, c)  # noqa: E731
        else:
            self.simulated_axes = set()

        self.axes: Dict[str, BaseAxis] = {
            name: axis_factory(name, self._create_axis_config(name)) for name in AXIS_NAMES
        }
        # Incremented by stop(); blocking operations abort when it changes
        self._stop_seq = 0
        self.logger = logging.getLogger(f"{__name__}.Gantry")

    @property
    def x_axis(self) -> BaseAxis:
        return self.axes["x"]

    @property
    def y_axis(self) -> BaseAxis:
        return self.axes["y"]

    @property
    def z_axis(self) -> BaseAxis:
        return self.axes["z"]

    def _create_axis_config(self, name: str) -> AxisConfig:
        ax = self.config.axes[name]
        m = self.config.motor
        k = ax.turns_per_mm
        return AxisConfig(
            serial_number=ax.serial,
            axis_type=_AXIS_TYPES[name],
            axis_number=ax.axis_number,
            motor_type=m.motor_type,
            pole_pairs=ax.pole_pairs or m.pole_pairs,
            torque_constant=m.torque_constant,
            encoder_cpr=ax.encoder_cpr or m.encoder_cpr,
            current_soft_max=m.current_soft_max,
            current_hard_max=m.current_hard_max,
            calibration_current=m.calibration_current,
            torque_soft_limit=m.torque_soft_limit,
            torque_hard_limit=m.torque_hard_limit,
            homing_torque_threshold=m.homing_torque_threshold,
            position_min=ax.min_mm * k,
            position_max=ax.max_mm * k,
            position_tolerance=self.config.position_tolerance_mm * k,
            arrive_tolerance=max(self.config.arrive_tolerance_mm, self.config.position_tolerance_mm) * k,
            motion_profile=MotionProfile(
                velocity_limit=max(self._speed(name, 1), self._speed(name, -1)) * k,
                acceleration_limit=self._accel(name) * k,
                deceleration_limit=self._decel(name) * k,
            ),
            homing_direction=ax.homing_direction,
            homing_velocity=ax.homing_speed_mm_s * k,
            homing_backoff_turns=ax.homing_backoff_mm * k,
            move_timeout=self.config.move_timeout_s,
            homing_timeout=self.config.homing_timeout_s,
            watchdog_timeout=self.config.watchdog_timeout_s,
            loop_settings={path: getattr(ax, key) for key, path in LOOP_SETTINGS.items()
                           if getattr(ax, key) is not None},
        )

    def _k(self, name: str) -> float:
        return self.config.axes[name].turns_per_mm

    def _speed(self, name: str, delta_mm: float) -> float:
        """Configured speed for a move of delta_mm (toward home -> speed_to_home_mm_s if set; Z: up), capped."""
        ax = self.config.axes[name]
        v = ax.speed_to_home_mm_s if delta_mm < 0 and ax.speed_to_home_mm_s else ax.speed_mm_s
        return min(v, ax.max_speed_mm_s) if ax.max_speed_mm_s else v

    def _accel(self, name: str) -> float:
        ax = self.config.axes[name]
        return min(ax.accel_mm_s2, ax.max_accel_mm_s2) if ax.max_accel_mm_s2 else ax.accel_mm_s2

    @staticmethod
    def _move_time(d: float, v: float, a: float, dcl: float) -> float:
        """Duration of a trapezoid move of d at top speed v, ramp up a, ramp down dcl (triangle if short)."""
        if d >= v * v / (2 * a) + v * v / (2 * dcl):
            return d / v + v / (2 * a) + v / (2 * dcl)
        v_peak = math.sqrt(2 * d * a * dcl / (a + dcl))
        return v_peak / a + v_peak / dcl

    def read_motion(self, name: str) -> Dict[str, float]:
        """What the drive is using now, in mm/s and mm/s^2 (trajectory limits of the last move)."""
        k = self._k(name)
        return {key: v / k for key, v in self.axes[name.lower()].read_motion().items()}

    def reload_settings(self, path: Path) -> List[str]:
        """
        Re-read gantry.json and apply speeds, ramps, loop gains, scale, limits and tolerances now - no restart.
        Wiring settings (serial, axis number, enabled) still need a restart. Returns what changed.
        """
        from .config import load_gantry_config
        new = load_gantry_config(path)
        changed: List[str] = []
        for name, ax in self.axes.items():
            if ax.status.state in (AxisState.MOVING, AxisState.HOMING):
                raise RuntimeError(f"{name.upper()} is moving")
        for name in AXIS_NAMES:
            cur, nxt = self.config.axes[name], new.axes[name]
            if (cur.serial, cur.axis_number, cur.enabled) != (nxt.serial, nxt.axis_number, nxt.enabled):
                changed.append(f"{name.upper()}: wiring changed in gantry.json - restart the app to use it")
            diff = [f.name for f in dataclasses.fields(cur)
                    if f.name not in ("serial", "axis_number", "enabled") and getattr(cur, f.name) != getattr(nxt, f.name)]
            for key in diff:
                setattr(cur, key, getattr(nxt, key))
            if diff:
                changed.append(f"{name.upper()}: " + ", ".join(diff))
        for key in ("speed_percent", "arrive_tolerance_mm", "position_tolerance_mm", "z_safe_mm"):
            if getattr(self.config, key) != getattr(new, key):
                setattr(self.config, key, getattr(new, key))
                changed.append(key)
        self.speed_factor = min(max(float(self.config.speed_percent), 1.0), 100.0) / 100
        for name, axis in self.axes.items():
            axis.config = self._create_axis_config(name)
            if axis.is_connected:
                axis.apply_motion_config()  # speeds, ramps, loop gains / quiet hold -> the drive
        self.logger.info("Settings reloaded from gantry.json: " + ("; ".join(changed) if changed else "no changes"))
        return changed

    def _decel(self, name: str) -> float:
        ax = self.config.axes[name]
        d = ax.decel_mm_s2 or ax.accel_mm_s2
        return min(d, ax.max_decel_mm_s2) if ax.max_decel_mm_s2 else d

    def set_limits(self, name: str, max_speed_mm_s: Optional[float], max_accel_mm_s2: Optional[float],
                   max_decel_mm_s2: Optional[float], save_to: Optional[Path] = None) -> None:
        """Set an axis' speed / ramp limits (None = no limit). Speeds and ramps above them are lowered to them."""
        name = name.lower()
        ax = self.config.axes[name]
        caps = {"max_speed_mm_s": max_speed_mm_s, "max_accel_mm_s2": max_accel_mm_s2, "max_decel_mm_s2": max_decel_mm_s2}
        for key, v in caps.items():
            if v is not None and not 0 < float(v) <= 5000:
                raise ValueError(f"{key} must be between 0 and 5000")
        for key, v in caps.items():
            setattr(ax, key, None if v is None else float(v))
            if save_to is not None and v is not None:
                save_axis_value(name, key, float(v), save_to)
        # Bring the settings within the new limits
        lowered = {}
        for key, cap in (("speed_mm_s", max_speed_mm_s), ("speed_to_home_mm_s", max_speed_mm_s),
                         ("accel_mm_s2", max_accel_mm_s2), ("decel_mm_s2", max_decel_mm_s2)):
            v = getattr(ax, key)
            if cap is not None and v is not None and v > cap:
                setattr(ax, key, float(cap))
                lowered[key] = float(cap)
                if save_to is not None:
                    save_axis_value(name, key, float(cap), save_to)
        self.axes[name].config = self._create_axis_config(name)
        if self.axes[name].is_connected:
            self.axes[name].apply_motion_config()
        self.logger.info(f"{name.upper()} limits: speed {max_speed_mm_s}, ramp up {max_accel_mm_s2}, "
                         f"ramp down {max_decel_mm_s2}" + (f" (lowered {lowered})" if lowered else ""))

    def set_motion(self, name: str, speed_mm_s: float, accel_mm_s2: float, decel_mm_s2: Optional[float] = None,
                   speed_to_home_mm_s: Optional[float] = None, save_to: Optional[Path] = None) -> None:
        """Change an axis' speed / ramps now (and in gantry.json if save_to)."""
        name = name.lower()
        values = {"speed_mm_s": speed_mm_s, "accel_mm_s2": accel_mm_s2, "decel_mm_s2": decel_mm_s2,
                  "speed_to_home_mm_s": speed_to_home_mm_s}
        for key, v in values.items():
            if v is not None and not 0 < float(v) <= 5000:
                raise ValueError(f"{key} must be between 0 and 5000")
        ax = self.config.axes[name]
        for key, cap_key in (("speed_mm_s", "max_speed_mm_s"), ("speed_to_home_mm_s", "max_speed_mm_s"),
                             ("accel_mm_s2", "max_accel_mm_s2"), ("decel_mm_s2", "max_decel_mm_s2")):
            cap, v = getattr(ax, cap_key), values[key]
            if cap and v is not None and float(v) > cap + 1e-9:
                raise ValueError(f"{name.upper()} {key.replace('_mm_s2', '').replace('_mm_s', '')} {v:g} is above "
                                 f"its limit {cap:g} - raise the limit first")
        if self.axes[name].status.state == AxisState.MOVING:
            raise RuntimeError(f"{name.upper()} is moving")
        ax = self.config.axes[name]
        for key, v in values.items():
            setattr(ax, key, None if v is None else float(v))
            if save_to is not None and v is not None:
                save_axis_value(name, key, float(v), save_to)
        self.axes[name].config = self._create_axis_config(name)
        self.axes[name].apply_motion_config()
        self.logger.info(f"{name.upper()} motion: speed {ax.speed_mm_s:g} mm/s"
                         + (f" (to home {ax.speed_to_home_mm_s:g})" if ax.speed_to_home_mm_s else "")
                         + f", accel {ax.accel_mm_s2:g}, decel {self._decel(name):g} mm/s2")

    def _stop_check(self) -> Callable[[], bool]:
        """Return a callable that becomes True once stop() is called."""
        seq = self._stop_seq
        return lambda: self._stop_seq != seq

    # ========================================================================
    # CONNECTION
    # ========================================================================

    def connect(self) -> bool:
        """Connect all three axes. All-or-nothing."""
        self.logger.info("Connecting to gantry axes...")
        connected = []
        for name, axis in self.axes.items():
            if not axis.connect():
                self.logger.error(f"Failed to connect to {name.upper()} axis")
                for other in connected:
                    other.disconnect()
                self.state = GantryState.DISCONNECTED
                return False
            connected.append(axis)
        self.state = GantryState.IDLE
        self.is_homed = False
        self.logger.info("All axes connected" + (" (SIMULATION)" if self.simulated else ""))
        return True

    def disconnect(self):
        self.logger.info("Disconnecting gantry...")
        for axis in self.axes.values():
            axis.disconnect()
        self.state = GantryState.DISCONNECTED
        self.is_homed = False

    @property
    def is_connected(self) -> bool:
        return self.state != GantryState.DISCONNECTED

    # ========================================================================
    # HOMING
    # ========================================================================

    def find_limits(self, names: Optional[List[str]] = None,
                    save_to: Optional[Path] = None) -> Optional[Dict[str, float]]:
        """
        Limit search: each axis stalls into its home-end stop (-> 0) and then its far stop.
        max_mm becomes the measured usable travel (applied now; written to save_to if given).
        Simulated stand-ins are just homed. Ends with X/Y back at home. Returns {axis: max_mm}.
        """
        if not self.is_connected:
            self.logger.error("Gantry not connected")
            return None
        names = [n.lower() for n in (names or self.config.home_order)]
        e_stop = self._stop_check()
        self.state = GantryState.HOMING
        self.is_homed = False
        found: Dict[str, float] = {}

        def failed():
            self.state = GantryState.STOPPED if e_stop() else GantryState.ERROR
            return None
        def measure(name: str):
            axis = self.axes[name]
            if name in self.simulated_axes and not self.simulated:
                return axis.home(e_stop) and "stand-in"  # stand-in: nothing to measure
            self.logger.info(f"{name.upper()}: finding both end stops")
            return axis.find_travel(e_stop)

        travel: Dict[str, object] = {}
        for stage in self._stages(names):
            ok = self._parallel(stage, lambda n: self._prepare(n, e_stop), e_stop)
            if not all(ok.values()):
                return failed()
            res = self._parallel(stage, measure, e_stop)
            bad = [n for n, v in res.items() if not v or (not isinstance(v, str) and v <= 0)]
            if bad:
                self.logger.error("Limit search failed: " + ", ".join(n.upper() for n in bad))
                return failed()
            travel.update(res)
            if "z" in stage and not isinstance(res.get("z"), str):
                # Z's search ends at its bottom: needle back up before anything moves sideways
                if not self.move_to(z=0.0, wait=True, require_homed=False):
                    return failed()
        for name, usable in travel.items():
            if isinstance(usable, str):
                continue
            axis = self.axes[name]
            max_mm = int(usable / self._k(name) * 10) / 10  # round down to 0.1 mm
            self.config.axes[name].max_mm = max_mm
            axis.config = self._create_axis_config(name)
            found[name] = max_mm
            if save_to is not None:
                save_axis_value(name, "max_mm", max_mm, save_to)
            self.logger.info(f"{name.upper()}: travel 0..{max_mm:.1f} mm "
                             f"({usable:.3f} turns at {self._k(name):.6g} turns/mm)")
        self.is_homed = all(a.status.is_homed for a in self.axes.values())
        self.state = GantryState.IDLE
        back = {n: 0.0 for n in found if n in ("x", "y")}
        # Only the measured axes need to be homed for this (a single-axis search leaves the others as they are)
        if back and not self.move_to(**back, wait=True, require_homed=False):
            return failed()
        return found

    # ========================================================================
    # TUNING
    # ========================================================================

    def read_tuning(self, name: str) -> Dict[str, float]:
        return self.axes[name.lower()].read_tuning()

    def apply_tuning(self, name: str, values: Dict[str, float], save_to: Optional[Path] = None) -> None:
        """Write loop gains to the axis now, keep them in the config (and gantry.json if save_to)."""
        name = name.lower()
        for key, value in values.items():
            if key not in GAIN_LIMITS:
                raise ValueError(f"unknown gain '{key}'")
            _, lo, hi = GAIN_LIMITS[key]
            if not lo <= float(value) <= hi:
                raise ValueError(f"{key} {value} outside {lo}..{hi}")
        axis = self.axes[name]
        axis.apply_tuning(values)
        for key, value in values.items():
            setattr(self.config.axes[name], key, float(value))
            axis.config.loop_settings[LOOP_SETTINGS[key]] = float(value)
            if save_to is not None:
                save_axis_value(name, key, float(value), save_to)

    def step_test(self, name: str, size_mm: float = 2.0) -> dict:
        """
        Tuning test on one axis: hold, move size_mm, move back, sampled fast. Goes toward the side
        with room when homed. Returns the trace in mm (relative to the start) and the step metrics.
        """
        name = name.lower()
        axis, ax = self.axes[name], self.config.axes[name]
        if not self.is_connected:
            return {"aborted": "gantry not connected"}
        size = abs(size_mm)
        if axis.status.is_homed:
            pos = self.get_position()
            here = getattr(pos, name)
            if here + size <= ax.max_mm:
                pass
            elif here - size >= ax.min_mm:
                size = -size
            else:
                return {"aborted": f"no room for a {size_mm:g} mm move at {here:.1f} mm"}
        k = self._k(name)
        sense = axis._sign  # coordinate +  ->  motor direction
        # The same speeds and ramps a normal move would use (direction-specific speed, slider applied)
        f = self.speed_factor
        v_out, v_back = self._speed(name, size) * f, self._speed(name, -size) * f
        accel_mm_s2, decel_mm_s2 = self._accel(name) * f, self._decel(name) * f
        speed_mm_s = max(v_out, v_back)
        settle = max(self._move_time(abs(size), v, accel_mm_s2, decel_mm_s2) for v in (v_out, v_back)) + 1.0
        self.state = GantryState.MOVING
        raw = axis.step_response(size * k * sense, self._stop_check(), settle_s=settle,
                                 velocity=(v_out * k, v_back * k), accel=accel_mm_s2 * k, decel=decel_mm_s2 * k)
        self.state = GantryState.IDLE
        out = {"size_mm": size, "gains": axis.read_tuning(), "aborted": raw["aborted"],
               "settings": {"speed_out": v_out, "speed_back": v_back, "accel": accel_mm_s2, "decel": decel_mm_s2}}
        if raw["start"] is None:
            return out
        start, step = raw["start"], raw["step"]
        to_mm = 1 / (k * sense)
        out["t"] = raw["t"]
        out["pos_mm"] = [(p - start) * to_mm for p in raw["pos"]]
        out["vel_mm_s"] = [v * to_mm for v in raw.get("vel", [])]
        marks = raw["marks"]
        out["target_mm"] = [size if len(marks) > 1 and marks[1] <= t < (marks[2] if len(marks) > 2 else 1e9) else 0.0
                            for t in raw["t"]]
        if raw["aborted"] or len(marks) < 3:
            return out
        up = analyze_step(raw["t"], raw["pos"], marks[1], start, start + step, marks[2])
        back = analyze_step(raw["t"], raw["pos"], marks[2], start + step, start)
        mm = abs(to_mm)
        # Commanded move time (trapezoid with the real speed / ramps): settling is judged after it
        move_s = self._move_time(abs(size), speed_mm_s, accel_mm_s2, decel_mm_s2)
        settle = max(up["settle_s"], back["settle_s"])
        out["metrics"] = {
            "overshoot_pct": max(up["overshoot_pct"], back["overshoot_pct"]),
            "settle_s": settle,
            "move_s": move_s,
            "settle_after_move_s": max(0.0, settle - move_s),
            "oscillations": max(up["oscillations"], back["oscillations"]),
            "error_mm": max(abs(up["ss_error"]), abs(back["ss_error"])) * mm,
            "out": describe("out ", size, {**up, "ss_error": up["ss_error"] * to_mm}),
            "back": describe("back", -size, {**back, "ss_error": back["ss_error"] * to_mm}),
        }
        return out

    def set_home_here(self, names: Optional[List[str]] = None) -> bool:
        """Manual homing: the current position of these axes (default all) becomes 0."""
        names = [n.lower() for n in (names or AXIS_NAMES)]
        ok = all([self.axes[n].set_home_here() for n in names])
        self.is_homed = all(a.status.is_homed for a in self.axes.values())
        if ok:
            self.logger.info(f"Home set at the current position: {', '.join(n.upper() for n in names)}")
        return ok

    def prepare_all(self) -> bool:
        """Start-up calibration (encoder offset) on every axis that needs it. Motors turn slightly."""
        if not self.is_connected:
            self.logger.error("Gantry not connected")
            return False
        e_stop = self._stop_check()
        self.state = GantryState.CALIBRATING
        for name in self.config.home_order:
            if not self._prepare(name.lower(), e_stop):
                self.state = GantryState.STOPPED if e_stop() else GantryState.ERROR
                return False
        self.state = GantryState.IDLE
        self.logger.info("Start-up calibration done")
        return True

    @property
    def speed_percent(self) -> float:
        return self.speed_factor * 100

    def set_speed_percent(self, percent: float) -> None:
        """Speed setting for all following moves, 1-100 % of the configured axis speeds."""
        self.speed_factor = min(max(float(percent), 1.0), 100.0) / 100
        self.logger.info(f"Speed set to {self.speed_percent:.0f} %")

    @property
    def manual_homing(self) -> bool:
        return self.config.homing_mode == "manual"

    def home_all(self, home_order: Optional[List[str]] = None) -> bool:
        """Home all axes in order (default config.home_order: Z first)."""
        if self.manual_homing:
            if self.is_homed:
                self.logger.info("Homing is manual and home is set - nothing to do")
                return True
            self.logger.error("Homing is manual: put the gantry at the home corner and click Set home here")
            return False
        order = [n.lower() for n in (home_order or self.config.home_order)]
        stages = self._stages(order)
        self.logger.info("Homing gantry: " + " then ".join("+".join(n.upper() for n in s) for s in stages))
        self.state = GantryState.HOMING
        self.is_homed = False
        e_stop = self._stop_check()
        for stage in stages:
            ok = self._parallel(stage, lambda n: self._prepare(n, e_stop), e_stop)
            if all(ok.values()):
                ok = self._parallel(stage, lambda n: self.axes[n].home(e_stop), e_stop)
            if not all(ok.values()):
                self.logger.error("Failed to home " + ", ".join(n.upper() for n, v in ok.items() if not v))
                self.state = GantryState.STOPPED if e_stop() else GantryState.ERROR
                return False
        self.is_homed = True
        self.state = GantryState.IDLE
        self.logger.info("Gantry homing complete")
        return True

    @staticmethod
    def _stages(names: List[str]) -> List[List[str]]:
        """Z on its own first (needle up before anything moves sideways), then the other axes together."""
        first = [n for n in names if n == "z"]
        rest = [n for n in names if n != "z"]
        return [s for s in (first, rest) if s]

    def _parallel(self, names: List[str], fn: Callable[[str], object], e_stop) -> Dict[str, object]:
        """Run fn(axis) for these axes at the same time (each axis is its own board). If one fails, the others
        are stopped too. Returns {axis: result}; an exception counts as a failure (False)."""
        results: Dict[str, object] = {}

        def run(n: str):
            try:
                results[n] = fn(n)
            except Exception as e:  # noqa: BLE001 - reported per axis
                self.logger.error(f"{n.upper()}: {e}")
                results[n] = False
            if not results[n] and len(names) > 1 and not e_stop():
                self.stop()  # one axis failed: don't leave the others running on their own
        if len(names) == 1:
            run(names[0])
            return results
        threads = [threading.Thread(target=run, args=(n,), name=f"{fn.__name__}-{n}", daemon=True) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def _prepare(self, name: str, e_stop) -> bool:
        """Start-up calibration (ODrive encoder offset after power-up) before the first homing."""
        axis = self.axes[name]
        if axis.is_prepared:
            return True
        self.logger.info(f"{name.upper()}: start-up calibration")
        if not axis.prepare(e_stop):
            self.logger.error(f"{name.upper()}: start-up calibration failed - see the axis errors")
            return False
        return True

    # ========================================================================
    # CALIBRATION
    # ========================================================================

    def calibration_status(self) -> Dict[str, dict]:
        out = {}
        for n, a in self.axes.items():
            st = a.calibration_status()
            st["stand_in"] = n in self.simulated_axes and not self.simulated
            out[n] = st
        return out

    def calibrate(self, name: str, kind: str) -> bool:
        """Run "motor", "encoder_offset" or "full" calibration on one axis (STOP aborts it)."""
        if not self.is_connected:
            self.logger.error("Gantry not connected")
            return False
        axis = self.axes[name.lower()]
        self.state = GantryState.CALIBRATING
        ok = axis.run_calibration(kind, self._stop_check())
        self.state = GantryState.IDLE if ok else GantryState.ERROR
        return ok

    def save_to_board(self, name: str) -> bool:
        """Save that axis' board config (e.g. a new motor calibration) to flash; the board reboots."""
        axis = self.axes[name.lower()]
        ok = axis.save_to_board()
        if ok:
            axis.status.is_homed = False
            self.is_homed = all(a.status.is_homed for a in self.axes.values())
        return ok

    @staticmethod
    def rescaled_turns_per_mm(current: float, commanded_mm: float, measured_mm: float) -> float:
        """New scale from a test move: the axis was told to go commanded_mm and really went measured_mm."""
        if commanded_mm <= 0 or measured_mm <= 0:
            raise ValueError("Distances must be positive")
        ratio = commanded_mm / measured_mm
        if not 0.1 <= ratio <= 10:
            raise ValueError(f"Measured {measured_mm:g} mm for a {commanded_mm:g} mm move - "
                             f"more than 10x off, check the measurement")
        return current * ratio

    def set_turns_per_mm(self, name: str, value: float) -> None:
        """Change one axis' scale while running. Homing stays valid (positions are kept in motor turns)."""
        name = name.lower()
        if not value > 0:
            raise ValueError("turns_per_mm must be positive")
        if self.axes[name].status.state == AxisState.MOVING:
            raise RuntimeError(f"{name.upper()} is moving")
        old = self.config.axes[name].turns_per_mm
        self.config.axes[name].turns_per_mm = float(value)
        axis = self.axes[name]
        axis.config = self._create_axis_config(name)
        axis.apply_motion_config()
        self.logger.info(f"{name.upper()} scale {old:.6g} -> {value:.6g} turns/mm ({1 / value:.4g} mm/turn)")

    def home_axis(self, name: str) -> bool:
        if self.manual_homing:
            self.logger.error("Homing is manual: put the axis at its home end and click Set home here")
            return False
        self.state = GantryState.HOMING
        e_stop = self._stop_check()
        ok = self._prepare(name.lower(), e_stop) and self.axes[name.lower()].home(e_stop)
        self.is_homed = all(a.status.is_homed for a in self.axes.values())
        self.state = GantryState.IDLE if ok else GantryState.ERROR
        return ok

    # ========================================================================
    # MOTION
    # ========================================================================

    def check_target(self, x: Optional[float] = None, y: Optional[float] = None,
                     z: Optional[float] = None) -> List[str]:
        """Return a list of limit violations for the target (empty if OK)."""
        errors = []
        for name, value in (("x", x), ("y", y), ("z", z)):
            if value is None:
                continue
            ax = self.config.axes[name]
            if self.axes[name].status.is_homed and not (ax.min_mm - 1e-6 <= value <= ax.max_mm + 1e-6):
                errors.append(f"{name.upper()}={value:.2f} mm outside [{ax.min_mm}, {ax.max_mm}]")
        return errors

    def move_to(self, x: Optional[float] = None, y: Optional[float] = None,
                z: Optional[float] = None, speed: Optional[float] = None,
                wait: bool = False, require_homed: bool = True) -> bool:
        """
        Move to an absolute position in mm (None keeps that axis where it is).
        All targets are validated before any axis moves. ``speed`` (mm/s)
        applies per axis. With ``wait=True`` blocks until the move finishes.
        """
        targets = {n: (v, speed, None, None) for n, v in (("x", x), ("y", y), ("z", z)) if v is not None}
        return self._start_moves(targets, wait, require_homed)

    def _start_moves(self, targets: Dict[str, Tuple[float, Optional[float], Optional[float], Optional[float]]],
                     wait: bool, require_homed: bool = True) -> bool:
        """
        targets: axis -> (position mm, speed mm/s, accel mm/s^2, decel mm/s^2; None = the axis' own).
        Validates everything before commanding any axis (all-or-nothing).
        """
        if not self.is_connected:
            self.logger.error("Gantry not connected")
            return False
        if require_homed and not self.is_homed:
            self.logger.error("Gantry not homed")
            return False
        faulted = [n for n, a in self.axes.items() if a.is_faulted]
        if faulted:
            self.logger.error(f"Axes faulted: {faulted} - reset first")
            return False
        errors = self.check_target(**{n: t[0] for n, t in targets.items()})
        if errors:
            self.logger.error("Move rejected: " + "; ".join(errors))
            return False
        if not targets:
            return True

        self.state = GantryState.MOVING
        f = self.speed_factor
        here = self.get_position()
        used = []
        for name, (value, speed, accel, decel) in targets.items():
            k = self._k(name)
            # Same factor on every axis keeps vector moves straight
            speed = (speed or self._speed(name, value - getattr(here, name))) * f
            accel = (accel or self._accel(name)) * f
            decel = (decel or self._decel(name)) * f
            used.append(f"{name.upper()}={value:.2f} ({speed:.0f} mm/s, ramp {accel:.0f}/{decel:.0f} mm/s2)")
            if not self.axes[name].move_to_position(value * k, speed * k, accel * k, decel * k):
                self.logger.error(f"{name.upper()} axis rejected move - stopping all axes")
                self.stop()
                self.state = GantryState.ERROR
                return False
        self.logger.info("Moving to " + ", ".join(used) + f" [speed {self.speed_percent:.0f} %]")
        return self.wait_for_moves() if wait else True

    def move_relative(self, dx: float = 0, dy: float = 0, dz: float = 0,
                      speed: Optional[float] = None, wait: bool = False,
                      require_homed: bool = True) -> bool:
        current = self.get_position()
        return self.move_to(
            x=current.x + dx if dx else None,
            y=current.y + dy if dy else None,
            z=current.z + dz if dz else None,
            speed=speed, wait=wait, require_homed=require_homed,
        )

    def jog(self, axis: str, distance: float, speed: Optional[float] = None, wait: bool = True) -> bool:
        """Jog one axis by ``distance`` mm. Allowed before homing."""
        name = axis.lower()
        if name not in self.axes:
            self.logger.error(f"Invalid axis: {axis}")
            return False
        return self.move_relative(**{f"d{name}": distance}, speed=speed, wait=wait, require_homed=False)

    def safe_move_to(self, x: Optional[float] = None, y: Optional[float] = None,
                     z: Optional[float] = None, speed: Optional[float] = None) -> bool:
        """
        Collision-safe travel: raise Z to z_safe (if below it), move XY,
        then move Z to the target. Blocks until done.
        """
        errors = self.check_target(x, y, z)
        if errors:
            self.logger.error("Move rejected: " + "; ".join(errors))
            return False

        cur = self.get_position()
        tol = self.config.position_tolerance_mm
        needs_xy = (x is not None and abs(x - cur.x) > tol) or (y is not None and abs(y - cur.y) > tol)
        if needs_xy:
            if cur.z > self.config.z_safe_mm + tol:
                if not self.move_to(z=self.config.z_safe_mm, wait=True):
                    return False
            # Coordinated XY: one straight diagonal line, both axes moving the whole way
            target = GantryPosition(cur.x if x is None else x, cur.y if y is None else y, None)
            if not self.move_linear(target, speed, wait=True):
                return False
        if z is not None:
            return self.move_to(z=z, wait=True)
        return True

    def raise_z(self) -> bool:
        """Move Z up to the safe height."""
        return self.move_to(z=self.config.z_safe_mm, wait=True)

    def move_linear(self, target: GantryPosition, speed: Optional[float] = None, wait: bool = False,
                    require_homed: bool = True) -> bool:
        """
        Coordinated straight-line move (vector move). Each axis gets the
        path speed and acceleration scaled by its share of the distance, so
        all axes start, accelerate, cruise and stop together and the tool
        tracks a straight line. ``target`` fields set to None keep that axis.
        ``speed`` is the path speed in mm/s (default: the slowest moving axis
        limit, so no axis is asked to exceed its own speed).
        """
        cur = self.get_position()
        goal = {"x": target.x, "y": target.y, "z": target.z}
        deltas = {n: goal[n] - getattr(cur, n) for n in AXIS_NAMES if goal[n] is not None}
        moving = {n: d for n, d in deltas.items() if abs(d) > self.config.position_tolerance_mm}
        if not moving:
            return True
        distance = sum(d * d for d in moving.values()) ** 0.5
        # Path speed / ramps such that no single axis exceeds its own limits (direction-specific speeds)
        max_speed = min(self._speed(n, d) * distance / abs(d) for n, d in moving.items())
        max_accel = min(self._accel(n) * distance / abs(d) for n, d in moving.items())
        max_decel = min(self._decel(n) * distance / abs(d) for n, d in moving.items())
        path_speed = min(speed, max_speed) if speed else min(min(self._speed(n, d) for n, d in moving.items()), max_speed)
        targets = {n: (goal[n], path_speed * abs(d) / distance, max_accel * abs(d) / distance,
                       max_decel * abs(d) / distance)
                   for n, d in moving.items()}
        return self._start_moves(targets, wait, require_homed)

    def wait_for_moves(self, timeout: Optional[float] = None) -> bool:
        """
        Block until no axis is moving. Returns False on fault, timeout or if
        stop() was called during the wait.
        """
        if timeout is None:
            timeout = max([self.config.move_timeout_s] + [a._move_timeout for a in self.axes.values()]) + 1
        stopped = self._stop_check()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if stopped():
                return False
            if not self.is_moving():
                faulted = {n: a.status.state.value for n, a in self.axes.items() if a.is_faulted}
                if faulted:
                    self.logger.error(f"Axis fault: {faulted}")
                    self.state = GantryState.ERROR
                    return False
                self.state = GantryState.IDLE
                return True
            time.sleep(0.02)
        self.logger.error("Move timeout")
        self.stop()
        self.state = GantryState.ERROR
        return False

    def stop(self, emergency: bool = False):
        """
        Stop all axes and abort any blocking move/homing in progress.
        Normal stop holds position; emergency de-energises the motors.
        """
        self._stop_seq += 1
        self.logger.warning(f"{'Emergency' if emergency else 'Normal'} stop")
        for axis in self.axes.values():
            axis.stop(emergency)
        if self.is_connected:
            self.state = GantryState.EMERGENCY_STOP if emergency else GantryState.STOPPED
        if emergency:
            # Motors unpowered: position can no longer be trusted
            self.is_homed = False
            for axis in self.axes.values():
                axis.status.is_homed = False

    def reset_all(self):
        """Clear axis faults (keeps homing unless an emergency stop happened)."""
        for axis in self.axes.values():
            axis.reset()
        if self.is_connected:
            self.state = GantryState.IDLE

    # ========================================================================
    # STATUS
    # ========================================================================

    def get_position(self) -> GantryPosition:
        return GantryPosition(*(self.axes[n].status.position / self._k(n) for n in AXIS_NAMES))

    def is_moving(self) -> bool:
        return any(a.status.state in (AxisState.MOVING, AxisState.HOMING) for a in self.axes.values())

    def get_status(self) -> Dict:
        pos = self.get_position()
        return {
            "state": self.state.value,
            "is_homed": self.is_homed,
            "simulated": self.simulated,
            "position": {"x": pos.x, "y": pos.y, "z": pos.z},
            "axes": {
                n: {
                    "state": a.status.state.value,
                    "position": a.status.position / self._k(n),
                    "velocity": a.status.velocity / self._k(n),
                    "torque": a.status.torque,
                    "is_homed": a.status.is_homed,
                    "errors": list(a.status.errors),
                }
                for n, a in self.axes.items()
            },
        }
