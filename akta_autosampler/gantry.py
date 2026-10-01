"""
3-axis gantry controller (mm coordinates).

Ported from ProteinMakerV5 system_configuration/odrive/gantry_class.py.
Coordinates are corner-origin: each axis is 0 at its homed end and runs to
``max_mm``. Z=0 is the top (homed) position and +Z moves the needle down.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional

from .config import AXIS_NAMES, GantryConfig
from .hardware.axis import AxisConfig, AxisState, AxisType, BaseAxis, MotionProfile

logger = logging.getLogger(__name__)

AxisFactory = Callable[[str, AxisConfig], BaseAxis]

_AXIS_TYPES = {"x": AxisType.X_AXIS, "y": AxisType.Y_AXIS, "z": AxisType.Z_AXIS}


class GantryState(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    HOMING = "homing"
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


def odrive_axis_factory() -> AxisFactory:
    from .hardware.odrive_axis import ODriveAxis, ODriveBoardPool
    pool = ODriveBoardPool()

    def make(name: str, axis_config: AxisConfig) -> BaseAxis:
        return ODriveAxis(axis_config, pool)
    return make


class Gantry:
    """Coordinates three axes for XYZ motion in mm."""

    def __init__(self, config: GantryConfig, axis_factory: Optional[AxisFactory] = None):
        self.config = config
        self.state = GantryState.DISCONNECTED
        self.is_homed = False
        self.simulated = config.simulate
        if axis_factory is None:
            axis_factory = simulated_axis_factory(config) if config.simulate else odrive_axis_factory()

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
            pole_pairs=m.pole_pairs,
            torque_constant=m.torque_constant,
            encoder_cpr=m.encoder_cpr,
            current_soft_max=m.current_soft_max,
            current_hard_max=m.current_hard_max,
            calibration_current=m.calibration_current,
            torque_soft_limit=m.torque_soft_limit,
            torque_hard_limit=m.torque_hard_limit,
            homing_torque_threshold=m.homing_torque_threshold,
            position_min=ax.min_mm * k,
            position_max=ax.max_mm * k,
            position_tolerance=self.config.position_tolerance_mm * k,
            motion_profile=MotionProfile(
                velocity_limit=ax.speed_mm_s * k,
                acceleration_limit=ax.accel_mm_s2 * k,
                deceleration_limit=ax.accel_mm_s2 * k,
            ),
            homing_direction=ax.homing_direction,
            homing_velocity=ax.homing_speed_mm_s * k,
            homing_backoff_turns=ax.homing_backoff_mm * k,
            move_timeout=self.config.move_timeout_s,
            homing_timeout=self.config.homing_timeout_s,
            watchdog_timeout=self.config.watchdog_timeout_s,
        )

    def _k(self, name: str) -> float:
        return self.config.axes[name].turns_per_mm

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

    def home_all(self, home_order: Optional[List[str]] = None) -> bool:
        """Home all axes in order (default config.home_order: Z first)."""
        order = home_order or self.config.home_order
        self.logger.info(f"Homing gantry in order: {order}")
        self.state = GantryState.HOMING
        self.is_homed = False
        e_stop = self._stop_check()
        for name in order:
            if not self.axes[name.lower()].home(e_stop):
                self.logger.error(f"Failed to home {name.upper()} axis")
                self.state = GantryState.STOPPED if e_stop() else GantryState.ERROR
                return False
        self.is_homed = True
        self.state = GantryState.IDLE
        self.logger.info("Gantry homing complete")
        return True

    def home_axis(self, name: str) -> bool:
        self.state = GantryState.HOMING
        ok = self.axes[name.lower()].home(self._stop_check())
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
        errors = self.check_target(x, y, z)
        if errors:
            self.logger.error("Move rejected: " + "; ".join(errors))
            return False

        targets = {n: v for n, v in (("x", x), ("y", y), ("z", z)) if v is not None}
        if not targets:
            return True

        self.state = GantryState.MOVING
        for name, value in targets.items():
            k = self._k(name)
            if not self.axes[name].move_to_position(value * k, speed * k if speed else None):
                self.logger.error(f"{name.upper()} axis rejected move - stopping all axes")
                self.stop()
                self.state = GantryState.ERROR
                return False
        self.logger.info(f"Moving to " + ", ".join(f"{n.upper()}={v:.2f}" for n, v in targets.items()))
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
            if not self.move_to(x=x, y=y, speed=speed, wait=True):
                return False
        if z is not None:
            return self.move_to(z=z, wait=True)
        return True

    def raise_z(self) -> bool:
        """Move Z up to the safe height."""
        return self.move_to(z=self.config.z_safe_mm, wait=True)

    def move_linear(self, target: GantryPosition, speed: float, wait: bool = False) -> bool:
        """Coordinated straight-line move: axis speeds scaled to arrive together."""
        current = self.get_position()
        dx, dy, dz = target.x - current.x, target.y - current.y, target.z - current.z
        distance = (dx ** 2 + dy ** 2 + dz ** 2) ** 0.5
        if distance < self.config.position_tolerance_mm:
            return True
        errors = self.check_target(target.x, target.y, target.z)
        if errors:
            self.logger.error("Move rejected: " + "; ".join(errors))
            return False

        move_time = distance / speed
        self.state = GantryState.MOVING
        for name, delta, value in (("x", dx, target.x), ("y", dy, target.y), ("z", dz, target.z)):
            if abs(delta) < self.config.position_tolerance_mm:
                continue
            k = self._k(name)
            if not self.axes[name].move_to_position(value * k, abs(delta) / move_time * k):
                self.stop()
                self.state = GantryState.ERROR
                return False
        return self.wait_for_moves() if wait else True

    def wait_for_moves(self, timeout: Optional[float] = None) -> bool:
        """
        Block until no axis is moving. Returns False on fault, timeout or if
        stop() was called during the wait.
        """
        timeout = self.config.move_timeout_s if timeout is None else timeout
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
