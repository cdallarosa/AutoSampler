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
from typing import Callable, Dict, List, Optional, Tuple

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
    arrive_tolerance: float = 0.01  # turns: a move is done within this of the target once stopped
    arrive_speed: float = 0.05  # turns/s: 'stopped' for the arrival check

    motion_profile: MotionProfile = field(default_factory=MotionProfile)

    # Homing
    homing_direction: int = -1  # motor sense of the home hard stop
    homing_velocity: float = 1.0  # turns/sec (magnitude)
    homing_backoff_turns: float = 0.2  # distance from hard stop to coordinate 0

    # Timeouts
    move_timeout: float = 30.0  # seconds
    homing_timeout: float = 60.0  # seconds
    watchdog_timeout: float = 0.5  # seconds
    loop_settings: Dict[str, float] = field(default_factory=dict)  # ODrive path (from axisN) -> value


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
        self._move_timeout: float = config.move_timeout

        self._prepared = False  # start-up calibration done since connect (see prepare())
        # Simulation keeps loop gains here (ODrive axes read/write the board)
        self._sim_tuning: Dict[str, float] = {"pos_gain": 20.0, "vel_gain": 1 / 6, "vel_integrator_gain": 1 / 3,
                                             "encoder_bandwidth": 1000.0, "enable_gain_scheduling": 0.0,
                                             "gain_scheduling_width": 0.001, "gain_scheduling_min_ratio": 0.0}

        # Motor turns = _offset + _sign * coordinate. Before homing the offset is 0 but the sign already
        # follows homing_direction, so a + jog moves away from the home end before and after homing.
        self._sign = -1 if config.homing_direction > 0 else 1
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
            msg = str(e) or type(e).__name__
            if isinstance(e, TimeoutError):
                msg = (f"no answer within {timeout:.0f} s - check USB + power, close the ODrive web GUI / "
                       f"odrivetool, and the serial in config/gantry.json")
            self.logger.error(f"Connection failed: {msg}")
            self.status.state = AxisState.DISCONNECTED
            self.status.errors.append(msg)
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
        self._prepared = False
        self.logger.info("Disconnected")

    # ------------------------------------------------------------------
    # Start-up calibration
    # ------------------------------------------------------------------

    @property
    def is_prepared(self) -> bool:
        return self._prepared

    def prepare(self, e_stop: EStop = None) -> bool:
        """
        Start-up calibration the hardware needs before closed-loop control
        (e.g. the ODrive encoder offset after power-up). Nothing for the simulator.
        """
        self._prepared = True
        return True

    def calibration_status(self) -> dict:
        return {"connected": self.is_connected, "simulated": True, "motor": True, "encoder_offset": True,
                "resistance": None, "inductance": None, "prepared": self._prepared}

    def run_calibration(self, kind: str, e_stop: EStop = None, timeout: float = 60.0) -> bool:
        """Simulated calibration: a short pause, always succeeds."""
        if kind not in ("motor", "encoder_offset", "full"):
            raise ValueError("kind must be motor, encoder_offset or full")
        self.status.state = AxisState.CALIBRATING
        time.sleep(0.8)
        if kind != "motor":
            self._prepared = True
        self.status.state = AxisState.IDLE
        self.logger.info(f"{kind.replace('_', ' ')} calibration complete (simulated)")
        return True

    def save_to_board(self) -> bool:
        return True

    def motion_blocker(self) -> Optional[str]:
        """Why this axis can't be commanded to move right now (None = it can)."""
        return None

    def set_home_here(self) -> bool:
        """Manual homing: make the current position coordinate 0 (positive = away from the home end)."""
        if not self.is_connected:
            self.logger.error("Not connected")
            return False
        if self.is_faulted:
            self.logger.error(f"Axis faulted ({self.status.state.value}); reset first")
            return False
        if self.status.state in (AxisState.MOVING, AxisState.HOMING):
            self.logger.error("Axis is moving")
            return False
        motor = self._hw_read()[0]
        with self._lock:
            self._sign = -1 if self.config.homing_direction > 0 else 1
            self._offset = motor
            self.status.position = 0.0
            self.status.target_position = None
            self.status.is_homed = True
        self.logger.info(f"Home set here (motor {motor:.3f} turns = 0)")
        return True

    def read_tuning(self) -> Dict[str, float]:
        """Control loop gains in use (keys as AxisSettings: pos_gain, vel_gain, ...)."""
        return dict(self._sim_tuning)

    def apply_tuning(self, values: Dict[str, float]) -> None:
        """Write loop gains to the controller now (RAM; gantry.json keeps them across connects)."""
        self._sim_tuning.update(values)

    def step_response(self, step: float, e_stop: EStop = None, hold_s: float = 0.4,
                      settle_s: float = 1.0, velocity: Optional[Tuple[float, float]] = None,
                      accel: Optional[float] = None, decel: Optional[float] = None) -> dict:
        """
        Tuning test: hold, move ``step`` turns, move back, sampling position and speed as fast as possible.
        ``velocity`` = (out, back) turns/s and ``accel`` / ``decel`` turns/s^2 are the move's real settings
        (default: the profile). On overspeed (1.5 x the commanded speed + 0.5 turns/s) or disarm the axis is
        switched off (IDLE); STOP holds it as usual.
        Returns {"t", "pos", "vel", "marks", "start", "step", "aborted"} in seconds / motor turns.
        """
        out = {"t": [], "pos": [], "vel": [], "marks": [], "start": None, "step": step, "aborted": None}
        if not self.is_connected or self.is_faulted:
            out["aborted"] = "axis not connected or faulted"
            return out
        blocker = self.motion_blocker()
        if blocker:
            out["aborted"] = blocker
            return out
        v_out, v_back = velocity or (self.config.motion_profile.velocity_limit,) * 2
        abort_vel = 1.5 * max(v_out, v_back) + 0.5
        self.status.state = AxisState.HOMING  # busy: keeps the monitor's move checks out of the way
        self.status.target_position = None
        try:
            if not self._hw_enter_closed_loop():
                out["aborted"] = "could not enter closed loop"
                return out
            start = self._hw_read()[0]
            out["start"] = start
            t0 = time.time()
            for target, dur, vel in ((start, hold_s, v_out), (start + step, settle_s, v_out), (start, settle_s, v_back)):
                self._hw_set_target(target, vel, accel, decel)
                out["marks"].append(time.time() - t0)
                end = time.time() + dur
                while time.time() < end:
                    p, v, _ = self._hw_read()
                    out["t"].append(time.time() - t0)
                    out["pos"].append(p)
                    out["vel"].append(v)
                    if abs(v) > abort_vel:
                        out["aborted"] = f"overspeed {v:+.2f} turns/s (limit {abort_vel:.2f})"
                    elif not self._hw_armed():
                        out["aborted"] = "the drive disarmed - " + self._hw_disarm_text()
                    elif e_stop and e_stop():
                        out["aborted"] = "stopped"
                    if out["aborted"]:
                        # Unstable gains: don't keep holding with them - de-energise this axis
                        if out["aborted"] == "stopped":
                            self._hw_hold()
                        else:
                            self._hw_idle()
                        self.logger.warning(f"Step test aborted: {out['aborted']}")
                        return out
                    time.sleep(0.001)
            return out
        except Exception as e:
            self._hw_hold()
            out["aborted"] = f"step test failed: {e}"
            return out
        finally:
            with self._lock:
                self.status.position = self._from_motor(self._hw_read()[0])
                if self.status.state == AxisState.HOMING:
                    self.status.state = AxisState.IDLE

    def read_motion(self) -> Dict[str, float]:
        """Speed / ramp limits in use (turns/s, turns/s^2). Simulation: the profile."""
        p = self.config.motion_profile
        return {"vel_limit": p.velocity_limit, "accel_limit": p.acceleration_limit, "decel_limit": p.deceleration_limit}

    def _hw_armed(self) -> bool:
        """Closed loop control still active (the drive did not disarm itself)."""
        return True

    def _hw_disarm_text(self) -> str:
        return ""

    def apply_motion_config(self) -> None:
        """Push changed speed/accel limits (self.config) to the hardware. Nothing to do in simulation."""

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

    def find_travel(self, e_stop: EStop = None) -> Optional[float]:
        """
        Find both hard stops by stalling into them (torque / stall, like homing):
        home-end stop first (-> coordinate 0 after the back-off), then the far stop.
        Backs off the far stop and returns the usable travel in turns (far stop
        minus the back-off, as a coordinate), or None on failure/e-stop.
        """
        if not self.home(e_stop):
            return None
        cfg = self.config
        home_stop = self._offset - self._sign * cfg.homing_backoff_turns
        self.status.state = AxisState.HOMING
        try:
            far = self._find_end_stop(-cfg.homing_direction * abs(cfg.homing_velocity), e_stop)
            self._hw_position_mode()
            if far is None:
                self._fail("Far end stop not found (timeout or e-stop)")
                return None
            usable = self._from_motor(far) - cfg.homing_backoff_turns
            target = self._to_motor(usable)
            self._hw_set_target(target, abs(cfg.homing_velocity))
            deadline = time.time() + 10.0
            while abs(self._hw_read()[0] - target) > cfg.position_tolerance:
                if e_stop and e_stop():
                    self._hw_hold()
                    self._fail("E-stop while backing off the far end")
                    return None
                if time.time() > deadline:
                    self._fail("Timeout backing off the far end stop")
                    return None
                time.sleep(self.MONITOR_PERIOD)
            with self._lock:
                self.status.position = self._from_motor(self._hw_read()[0])
                self.status.state = AxisState.IDLE
            self.logger.info(f"Travel: home-end stop {home_stop:.3f} turns, far stop {far:.3f} turns -> "
                             f"usable 0..{usable:.3f} turns")
            return usable
        except Exception as e:
            self._hw_hold()
            self._fail(f"Finding the far end failed: {e}")
            return None

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

    def move_to_position(self, position: float, velocity: Optional[float] = None,
                         accel: Optional[float] = None, decel: Optional[float] = None) -> bool:
        """
        Start a move to ``position`` (turns). ``velocity`` / ``accel`` / ``decel`` (turns/s,
        turns/s^2) override the profile for this move, capped at the profile
        limits; coordinated moves use them to scale each axis. Returns True if
        the command was accepted.
        """
        if not self.is_connected:
            self.logger.error("Not connected")
            return False
        if self.is_faulted:
            self.logger.error(f"Axis faulted ({self.status.state.value}); reset first")
            return False
        blocker = self.motion_blocker()
        if blocker:
            self.logger.error(blocker)
            return False
        if not self.status.is_homed:
            self.logger.warning("Axis not homed - position is relative to power-on")

        error = self.check_limits(position)
        if error:
            self.logger.error(error)
            return False

        try:
            profile = self.config.motion_profile
            vel = profile.velocity_limit if velocity is None else min(abs(velocity), profile.velocity_limit)
            acc = None if accel is None else min(abs(accel), profile.acceleration_limit)
            dec = None if decel is None else min(abs(decel), profile.deceleration_limit)
            if not self._hw_enter_closed_loop():
                return self._fail("Failed to enter closed loop control")
            with self._lock:
                self.status.target_position = position
                self.status.state = AxisState.MOVING
                self._move_start_time = time.time()
                # Trapezoid estimate (distance / speed + one accel ramp), doubled: slow moves get time
                est = abs(position - self.status.position) / max(vel, 1e-9) + vel / max(acc or profile.acceleration_limit, 1e-9)
                self._move_timeout = max(self.config.move_timeout, 2 * est + 5)
                self._hw_set_target(self._to_motor(position), vel, acc, dec)
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
            self.logger.debug("Emergency stop" if emergency else "Motion stopped")
        except Exception as e:
            self.logger.error(f"Stop command failed: {e}")

    def is_at_position(self, tolerance: Optional[float] = None) -> bool:
        """Arrived: within the arrival tolerance of the target and (practically) stopped."""
        if self.status.target_position is None:
            return False
        tol = self.config.arrive_tolerance if tolerance is None else tolerance
        close = abs(self.status.position - self.status.target_position) <= tol
        return close and abs(self.status.velocity) <= self.config.arrive_speed

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
                elif self._move_start_time and time.time() - self._move_start_time > self._move_timeout:
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

    def _hw_set_target(self, motor_pos: float, velocity: float, accel: Optional[float] = None,
                       decel: Optional[float] = None) -> None:
        """Trapezoidal move to ``motor_pos`` at ``velocity`` turns/s (``accel`` turns/s^2, None = profile)."""
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
