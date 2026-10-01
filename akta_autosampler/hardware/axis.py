"""
Hardware-independent axis base class.

Ported from ProteinMakerV5 system_configuration/odrive/odrive_class.py. The
motion state machine, homing algorithm and coordinate handling live here; the
ODrive and simulator subclasses only implement the small set of ``_hw_*``
primitives at the bottom of the class.

Coordinates
-----------
Each axis works in *turns* (motor revolutions). After homing, coordinate 0 is
the homed end of travel (hard stop + back-off) and positive coordinates move
away from it, so every axis runs 0..max. ``homing_direction`` (motor sense,
-1 or +1) selects which hard stop is home. For Z, home is the top, so +Z is
down.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import Event, Lock, Thread
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

EStop = Optional[Callable[[], bool]]


# ============================================================================
# ENUMS AND DATA CLASSES
# ============================================================================

class AxisType(Enum):
    """Axis identification"""
    X_AXIS = "X"
    Y_AXIS = "Y"
    Z_AXIS = "Z"


class AxisState(Enum):
    """Axis operational states"""
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    HOMING = "homing"
    MOVING = "moving"
    COMPLETED = "completed"
    ERROR = "error"
    TORQUE_LIMIT_REACHED = "torque_limit_reached"
    CALIBRATING = "calibrating"


# States that stay set until reset() is called
FAULT_STATES = (AxisState.ERROR, AxisState.TORQUE_LIMIT_REACHED)


@dataclass
class MotionProfile:
    """Motion control parameters"""
    velocity_limit: float = 10.0  # turns/sec
    acceleration_limit: float = 10.0  # turns/sec^2
    deceleration_limit: float = 10.0  # turns/sec^2


@dataclass
class AxisConfig:
    """Axis configuration parameters"""
    serial_number: str = ""
    axis_type: AxisType = AxisType.X_AXIS
    axis_number: int = 0  # 0 for axis0, 1 for axis1

    # Motor configuration. motor_type is an odrive.enums.MotorType member name;
    # it is resolved lazily so this module never imports odrive.
    motor_type: str = "PMSM_CURRENT_CONTROL"
    pole_pairs: int = 4
    torque_constant: float = 0.095
    encoder_cpr: int = 3070

    # Current/Torque limits
    current_soft_max: float = 30.0  # Amps
    current_hard_max: float = 49.0  # Amps
    calibration_current: float = 10.0  # Amps
    torque_soft_limit: float = 2.0  # Nm - trips a move
    torque_hard_limit: float = 4.0  # Nm - firmware torque clamp
    homing_torque_threshold: float = 1.5  # Nm - torque to detect the hard stop

    # Position limits in axis coordinates (turns, 0 = home). Enforced once homed.
    position_min: Optional[float] = None
    position_max: Optional[float] = None
    position_tolerance: float = 0.01  # turns

    motion_profile: MotionProfile = field(default_factory=MotionProfile)

    # Homing
    homing_direction: int = -1  # motor sense of the home hard stop
    homing_velocity: float = 1.0  # turns/sec (magnitude)
    homing_backoff_turns: float = 0.2  # distance from hard stop to coordinate 0

    # Timeouts
    move_timeout: float = 30.0  # seconds
    homing_timeout: float = 60.0  # seconds
    watchdog_timeout: float = 0.5  # seconds


@dataclass
class AxisStatus:
    """Current axis status (position/velocity in axis coordinates)"""
    state: AxisState = AxisState.DISCONNECTED
    position: float = 0.0  # turns
    velocity: float = 0.0  # turns/sec
    current: float = 0.0  # Amps
    torque: float = 0.0  # Nm
    target_position: Optional[float] = None
    is_homed: bool = False
    errors: List[str] = field(default_factory=list)
    last_update: datetime = field(default_factory=datetime.now)


# ============================================================================
# BASE AXIS
# ============================================================================

class BaseAxis:
    """Shared motion logic for real and simulated axes."""

    MONITOR_PERIOD = 0.01  # 100 Hz

    def __init__(self, config: AxisConfig):
        self.config = config
        self.status = AxisStatus()
        self._lock = Lock()
        self._monitoring_thread: Optional[Thread] = None
        self._stop_monitoring = Event()
        self._move_start_time: Optional[float] = None

        # Motor turns = _offset + _sign * coordinate. Identity until homed.
        self._sign = 1
        self._offset = 0.0

        self.logger = logging.getLogger(f"{type(self).__module__}.{config.axis_type.value}")

    # ------------------------------------------------------------------
    # Coordinate transform
    # ------------------------------------------------------------------

    def _to_motor(self, coord: float) -> float:
        return self._offset + self._sign * coord

    def _from_motor(self, motor: float) -> float:
        return self._sign * (motor - self._offset)

    @property
    def is_connected(self) -> bool:
        return self.status.state != AxisState.DISCONNECTED

    @property
    def is_faulted(self) -> bool:
        return self.status.state in FAULT_STATES

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> bool:
        """Connect to the hardware and start the monitor thread."""
        try:
            self.logger.info(f"Connecting {self.config.axis_type.value} axis...")
            self._hw_connect(timeout)
            self._start_monitoring()
            self.status.state = AxisState.IDLE
            self.logger.info(f"Connected to {self.config.axis_type.value} axis")
            return True
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            self.status.state = AxisState.DISCONNECTED
            self.status.errors.append(str(e))
            return False

    def disconnect(self):
        """Stop monitoring, put the axis in idle and release the hardware."""
        try:
            self._stop_monitoring.set()
            if self._monitoring_thread:
                self._monitoring_thread.join(timeout=2.0)
            self._hw_idle()
            self._hw_disconnect()
        except Exception as e:
            self.logger.error(f"Error during disconnect: {e}")
        self.status.state = AxisState.DISCONNECTED
        self.status.is_homed = False
        self.logger.info("Disconnected")

    # ------------------------------------------------------------------
    # Homing
    # ------------------------------------------------------------------

    def home(self, e_stop: EStop = None) -> bool:
        """
        Sensorless homing: drive toward the ``homing_direction`` hard stop at
        ``homing_velocity`` until stall/torque is detected, back off by
        ``homing_backoff_turns`` and make that point coordinate 0.
        """
        if not self.is_connected:
            self.logger.error("Not connected")
            return False

        cfg = self.config
        self.logger.info(f"Homing {cfg.axis_type.value} (direction {cfg.homing_direction:+d}, "
                         f"{cfg.homing_velocity} turns/s)")
        self.status.state = AxisState.HOMING
        self.status.is_homed = False
        self.status.target_position = None

        try:
            self._hw_clear_errors()
            if not self._hw_enter_closed_loop():
                return self._fail("Failed to enter closed loop control")

            stop_pos = self._find_end_stop(cfg.homing_direction * abs(cfg.homing_velocity), e_stop)
            self._hw_position_mode()
            if stop_pos is None:
                return self._fail("Hard stop not found (timeout or e-stop)")

            self._sign = -1 if cfg.homing_direction > 0 else 1
            self._offset = stop_pos + self._sign * cfg.homing_backoff_turns
            self.logger.info(f"Hard stop at {stop_pos:.3f} turns; zero at {self._offset:.3f} turns")

            # Back off to the new zero
            self._hw_set_target(self._offset, cfg.motion_profile.velocity_limit)
            deadline = time.time() + 10.0
            while abs(self._hw_read()[0] - self._offset) > cfg.position_tolerance:
                if e_stop and e_stop():
                    self._hw_hold()
                    return self._fail("E-stop during homing back-off")
                if time.time() > deadline:
                    return self._fail("Timeout backing off from hard stop")
                time.sleep(self.MONITOR_PERIOD)

            with self._lock:
                # Refresh now rather than waiting for the next monitor tick
                self.status.position = self._from_motor(self._hw_read()[0])
                self.status.is_homed = True
                self.status.state = AxisState.IDLE
            self.logger.info("Homing complete")
            return True

        except Exception as e:
            self._hw_hold()
            return self._fail(f"Homing failed: {e}")

    def _find_end_stop(self, velocity: float, e_stop: EStop) -> Optional[float]:
        """
        Drive at constant velocity until the axis stalls or torque exceeds
        ``homing_torque_threshold``. Returns the motor position of the stop.
        """
        cfg = self.config
        dt = self.MONITOR_PERIOD
        startup_grace = 0.5  # s - ignore stall while accelerating
        # Stalled = moving less than 20% of the commanded distance per sample
        stall_step = 0.2 * abs(velocity) * dt
        stall_samples_needed = 20

        self._hw_velocity_mode(velocity)
        start = time.time()
        last_pos = self._hw_read()[0]
        stalled = 0
        torque_samples: List[float] = []

        try:
            while time.time() - start < cfg.homing_timeout:
                if e_stop and e_stop():
                    self.logger.warning("E-stop triggered during homing")
                    return None

                time.sleep(dt)
                pos, _vel, current = self._hw_read()
                self._hw_feed()

                if time.time() - start < startup_grace:
                    last_pos = pos
                    continue

                stalled = stalled + 1 if abs(pos - last_pos) < stall_step else 0
                last_pos = pos
                if stalled >= stall_samples_needed:
                    self.logger.info(f"Hard stop detected (stall) at {pos:.3f} turns")
                    return pos

                torque_samples.append(abs(current * cfg.torque_constant))
                torque_samples = torque_samples[-10:]
                avg_torque = sum(torque_samples) / len(torque_samples)
                if avg_torque > cfg.homing_torque_threshold:
                    self.logger.info(f"Hard stop detected (torque {avg_torque:.2f} Nm) at {pos:.3f} turns")
                    return pos

            self.logger.error("Homing timeout")
            return None
        finally:
            self._hw_velocity_mode(0.0)

    # ------------------------------------------------------------------
    # Motion
    # ------------------------------------------------------------------

    def check_limits(self, position: float) -> Optional[str]:
        """Return an error message if ``position`` (turns) is out of limits, else None."""
        if not self.status.is_homed:
            return None  # limits are meaningless before homing
        lo, hi = self.config.position_min, self.config.position_max
        if lo is not None and position < lo - 1e-9:
            return f"{self.config.axis_type.value} target {position:.3f} below minimum {lo:.3f}"
        if hi is not None and position > hi + 1e-9:
            return f"{self.config.axis_type.value} target {position:.3f} above maximum {hi:.3f}"
        return None

    def move_to_position(self, position: float, velocity: Optional[float] = None) -> bool:
        """Start a move to ``position`` (turns). Returns True if the command was accepted."""
        if not self.is_connected:
            self.logger.error("Not connected")
            return False
        if self.is_faulted:
            self.logger.error(f"Axis faulted ({self.status.state.value}); reset first")
            return False
        if not self.status.is_homed:
            self.logger.warning("Axis not homed - position is relative to power-on")

        error = self.check_limits(position)
        if error:
            self.logger.error(error)
            return False

        try:
            vel = self.config.motion_profile.velocity_limit
            if velocity is not None:
                vel = min(abs(velocity), vel)
            if not self._hw_enter_closed_loop():
                return self._fail("Failed to enter closed loop control")
            with self._lock:
                self.status.target_position = position
                self.status.state = AxisState.MOVING
                self._move_start_time = time.time()
                self._hw_set_target(self._to_motor(position), vel)
            self.logger.debug(f"Moving to {position:.3f} turns at {vel:.3f} turns/s")
            return True
        except Exception as e:
            return self._fail(f"Move command failed: {e}")

    def move_relative(self, distance: float, velocity: Optional[float] = None) -> bool:
        """Move relative to the current position (turns)."""
        return self.move_to_position(self.status.position + distance, velocity)

    def stop(self, emergency: bool = False):
        """
        Stop motion. Normal stop holds the current position (motor stays
        energised). Emergency stop puts the axis in IDLE (motor unpowered -
        a Z axis without a brake may drop).
        """
        if not self.is_connected:
            return
        try:
            with self._lock:
                if emergency:
                    self._hw_idle()
                else:
                    self._hw_hold()
                self.status.target_position = None
                if not self.is_faulted:
                    self.status.state = AxisState.IDLE
            self.logger.info("Emergency stop" if emergency else "Motion stopped")
        except Exception as e:
            self.logger.error(f"Stop command failed: {e}")

    def is_at_position(self, tolerance: Optional[float] = None) -> bool:
        if self.status.target_position is None:
            return False
        tol = self.config.position_tolerance if tolerance is None else tolerance
        return abs(self.status.position - self.status.target_position) <= tol

    def reset(self):
        """Clear errors and faults and return to IDLE (keeps homing)."""
        if not self.is_connected:
            return
        try:
            self._hw_clear_errors()
            self._hw_hold()
        except Exception as e:
            self.logger.error(f"Reset failed: {e}")
        with self._lock:
            self.status.errors.clear()
            self.status.target_position = None
            self.status.state = AxisState.IDLE
        self.logger.info("Reset complete")

    def get_status(self) -> AxisStatus:
        with self._lock:
            return self.status

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _start_monitoring(self):
        self._stop_monitoring.clear()
        self._monitoring_thread = Thread(target=self._monitor_loop, daemon=True,
                                         name=f"axis-{self.config.axis_type.value}")
        self._monitoring_thread.start()

    def _monitor_loop(self):
        while not self._stop_monitoring.is_set():
            try:
                self._monitor_step()
            except Exception as e:
                self.logger.error(f"Monitoring error: {e}")
            time.sleep(self.MONITOR_PERIOD)

    def _monitor_step(self):
        cfg = self.config
        with self._lock:
            motor_pos, motor_vel, current = self._hw_read()
            st = self.status
            st.position = self._from_motor(motor_pos)
            st.velocity = self._sign * motor_vel
            st.current = current
            st.torque = current * cfg.torque_constant

            if st.state == AxisState.MOVING:
                if abs(st.torque) > cfg.torque_soft_limit:
                    # Sticky fault: hold position and stay faulted until reset()
                    self._hw_hold()
                    st.state = AxisState.TORQUE_LIMIT_REACHED
                    st.errors.append(f"Torque limit {st.torque:.2f} Nm at {st.position:.3f} turns")
                    self.logger.warning(st.errors[-1])
                elif self.is_at_position():
                    st.state = AxisState.COMPLETED
                elif self._move_start_time and time.time() - self._move_start_time > cfg.move_timeout:
                    self._hw_hold()
                    st.state = AxisState.ERROR
                    st.errors.append("Move timeout")
                    self.logger.error("Move timeout")

            st.last_update = datetime.now()
            self._hw_feed()

    def _fail(self, message: str) -> bool:
        self.logger.error(message)
        self.status.state = AxisState.ERROR
        self.status.errors.append(message)
        return False

    # ------------------------------------------------------------------
    # Hardware primitives (motor turns) - implemented by subclasses
    # ------------------------------------------------------------------

    def _hw_connect(self, timeout: float) -> None:
        raise NotImplementedError

    def _hw_disconnect(self) -> None:
        raise NotImplementedError

    def _hw_read(self) -> Tuple[float, float, float]:
        """Return (motor position turns, motor velocity turns/s, Iq current A)."""
        raise NotImplementedError

    def _hw_enter_closed_loop(self) -> bool:
        raise NotImplementedError

    def _hw_set_target(self, motor_pos: float, velocity: float) -> None:
        """Trapezoidal position move to ``motor_pos`` at ``velocity`` turns/s."""
        raise NotImplementedError

    def _hw_velocity_mode(self, velocity: float) -> None:
        """Switch to velocity control and command ``velocity`` turns/s."""
        raise NotImplementedError

    def _hw_position_mode(self) -> None:
        """Return to position control, holding the current position."""
        raise NotImplementedError

    def _hw_hold(self) -> None:
        """Hold the current position (decelerate and stop)."""
        raise NotImplementedError

    def _hw_idle(self) -> None:
        """De-energise the motor."""
        raise NotImplementedError

    def _hw_clear_errors(self) -> None:
        pass

    def _hw_feed(self) -> None:
        """Feed the hardware watchdog."""
        pass
