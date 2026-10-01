"""
ODrive axis driver.

Ported from ProteinMakerV5 system_configuration/odrive/odrive_class.py.
``odrive`` is imported lazily so simulation mode works without the package
(or the WinUSB driver) installed.

Targets the odrive 0.6.x Python package (ODrive Pro / S1 firmware); a few
attribute fallbacks for older firmware are kept from the original.
"""

import time
from threading import Lock
from typing import Dict, Optional, Set, Tuple

from .axis import AxisConfig, AxisState, BaseAxis


def _odrive():
    try:
        import odrive
        import odrive.enums
        import odrive.utils
    except ImportError as e:  # pragma: no cover - depends on install
        raise RuntimeError("The 'odrive' package is required for hardware mode "
                           "(pip install odrive), or run with --sim") from e
    return odrive


class ODriveBoardPool:
    """
    Shares one connection per ODrive board, so two axes on the same board
    (axis0/axis1) don't each run find_any() and board-level configuration.
    """

    def __init__(self):
        self._boards: Dict[str, object] = {}
        self._configured: Set[str] = set()
        self._lock = Lock()

    def get(self, serial: str, timeout: float = 10.0):
        with self._lock:
            if serial not in self._boards:
                odrive = _odrive()
                board = odrive.find_any(serial_number=serial, timeout=timeout)
                if not board:
                    raise RuntimeError(f"ODrive {serial} not found")
                self._boards[serial] = board
            board = self._boards[serial]
            if serial not in self._configured:
                self._configure_board(board)
                self._configured.add(serial)
            return board

    def release(self, serial: str):
        with self._lock:
            self._boards.pop(serial, None)
            self._configured.discard(serial)

    @staticmethod
    def _configure_board(odrv):
        from odrive.enums import Protocol
        odrv.config.dc_bus_overvoltage_trip_level = 50
        odrv.config.dc_bus_undervoltage_trip_level = 10.5
        odrv.config.dc_max_positive_current = 60
        odrv.config.dc_max_negative_current = -60
        try:
            odrv.can.config.protocol = Protocol.NONE  # CAN unused
        except AttributeError:
            pass


class ODriveAxis(BaseAxis):
    """One ODrive axis, identified by board serial number + axis0/axis1."""

    def __init__(self, config: AxisConfig, pool: Optional[ODriveBoardPool] = None):
        super().__init__(config)
        self._pool = pool or ODriveBoardPool()
        self._odrive = None
        self._axis = None

    # ------------------------------------------------------------------
    # Connection / configuration
    # ------------------------------------------------------------------

    def _hw_connect(self, timeout: float) -> None:
        self._odrive = self._pool.get(self.config.serial_number, timeout)
        if self.config.axis_number not in (0, 1):
            raise ValueError(f"Invalid axis number: {self.config.axis_number}")
        self._axis = getattr(self._odrive, f"axis{self.config.axis_number}")
        self._configure_axis()

    def _hw_disconnect(self) -> None:
        self._pool.release(self.config.serial_number)
        self._odrive = None
        self._axis = None

    def _configure_axis(self):
        from odrive.enums import ControlMode, EncoderId, InputMode, MotorType

        cfg = self.config
        odrv, axis = self._odrive, self._axis
        self.logger.info("Configuring axis...")

        # Motor
        motor_type = getattr(MotorType, cfg.motor_type, None)
        if motor_type is None:
            raise ValueError(f"Unknown motor_type '{cfg.motor_type}'. "
                             f"Options: {[m.name for m in MotorType]}")
        axis.config.motor.motor_type = motor_type
        axis.config.motor.pole_pairs = cfg.pole_pairs
        axis.config.motor.torque_constant = cfg.torque_constant
        axis.config.motor.current_soft_max = cfg.current_soft_max
        axis.config.motor.current_hard_max = cfg.current_hard_max
        axis.config.motor.calibration_current = cfg.calibration_current
        axis.config.motor.resistance_calib_max_voltage = 6

        # Encoder
        axis.config.load_encoder = EncoderId.INC_ENCODER0
        axis.config.commutation_encoder = EncoderId.INC_ENCODER0
        if hasattr(odrv, "inc_encoder0"):
            odrv.inc_encoder0.config.enabled = True
            odrv.inc_encoder0.config.cpr = cfg.encoder_cpr

        # Position control with trapezoidal trajectory
        axis.controller.config.control_mode = ControlMode.POSITION_CONTROL
        axis.controller.config.input_mode = InputMode.TRAP_TRAJ
        profile = cfg.motion_profile
        axis.controller.config.vel_limit = max(profile.velocity_limit, cfg.homing_velocity) * 1.2
        axis.controller.config.vel_limit_tolerance = 1.2
        axis.trap_traj.config.vel_limit = profile.velocity_limit
        axis.trap_traj.config.accel_limit = profile.acceleration_limit
        axis.trap_traj.config.decel_limit = profile.deceleration_limit

        # Firmware torque clamp (Nm)
        axis.config.torque_soft_min = -cfg.torque_hard_limit
        axis.config.torque_soft_max = cfg.torque_hard_limit

        # Watchdog: the monitor thread feeds it at 100 Hz. If Python hangs the
        # axis drops to idle after watchdog_timeout.
        axis.config.watchdog_timeout = cfg.watchdog_timeout
        axis.watchdog_feed()
        axis.config.enable_watchdog = True
        axis.watchdog_feed()

        self.logger.info("Axis configuration complete")

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    def _read_pos_vel(self) -> Tuple[float, float]:
        axis = self._axis
        if hasattr(axis, "pos_vel_mapper"):
            return axis.pos_vel_mapper.pos_rel, axis.pos_vel_mapper.vel
        if hasattr(axis, "encoder") and hasattr(axis.encoder, "pos_estimate"):
            return axis.encoder.pos_estimate, axis.encoder.vel_estimate
        return axis.controller.input_pos, 0.0

    def _read_current(self) -> float:
        motor = self._axis.motor
        try:
            if hasattr(motor, "foc"):
                return motor.foc.Iq_measured
            if hasattr(motor, "current_control"):
                return motor.current_control.Iq_measured
        except Exception:
            pass
        return 0.0

    def _hw_read(self) -> Tuple[float, float, float]:
        pos, vel = self._read_pos_vel()
        return pos, vel, self._read_current()

    def _hw_enter_closed_loop(self) -> bool:
        from odrive.enums import AxisState as ODriveAxisState
        from odrive.utils import request_state

        if self._axis.current_state == ODriveAxisState.CLOSED_LOOP_CONTROL:
            return True
        # Hold where we are so entering closed loop doesn't jump to a stale input_pos
        self._axis.controller.input_pos = self._read_pos_vel()[0]
        self._axis.watchdog_feed()
        request_state(self._axis, ODriveAxisState.CLOSED_LOOP_CONTROL)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            self._axis.watchdog_feed()
            if self._axis.current_state == ODriveAxisState.CLOSED_LOOP_CONTROL:
                return True
            time.sleep(0.05)
        self.logger.error(f"Closed loop failed. State: {self._axis.current_state}, "
                          f"error: {getattr(self._axis, 'active_errors', getattr(self._axis, 'error', '?'))}")
        return False

    def _hw_set_target(self, motor_pos: float, velocity: float) -> None:
        self._axis.trap_traj.config.vel_limit = velocity
        self._axis.controller.input_pos = motor_pos

    def _hw_velocity_mode(self, velocity: float) -> None:
        from odrive.enums import ControlMode, InputMode
        ctrl = self._axis.controller
        if ctrl.config.control_mode != ControlMode.VELOCITY_CONTROL:
            ctrl.input_vel = 0
            ctrl.config.vel_ramp_rate = 10.0  # turns/s^2
            ctrl.config.control_mode = ControlMode.VELOCITY_CONTROL
            ctrl.config.input_mode = InputMode.VEL_RAMP
        ctrl.input_vel = velocity

    def _hw_position_mode(self) -> None:
        from odrive.enums import ControlMode, InputMode
        ctrl = self._axis.controller
        ctrl.input_vel = 0
        # Set input_pos before switching mode so the axis doesn't jump
        ctrl.input_pos = self._read_pos_vel()[0]
        ctrl.config.control_mode = ControlMode.POSITION_CONTROL
        ctrl.config.input_mode = InputMode.TRAP_TRAJ

    def _hw_hold(self) -> None:
        if self._axis is not None:
            self._axis.controller.input_pos = self._read_pos_vel()[0]

    def _hw_idle(self) -> None:
        if self._axis is not None:
            from odrive.enums import AxisState as ODriveAxisState
            from odrive.utils import request_state
            request_state(self._axis, ODriveAxisState.IDLE)

    def _hw_clear_errors(self) -> None:
        try:
            self._axis.clear_errors()
        except AttributeError:
            try:
                self._odrive.clear_errors()
            except Exception:
                pass

    def _hw_feed(self) -> None:
        if self._axis is not None:
            self._axis.watchdog_feed()

    # ------------------------------------------------------------------
    # Extras kept from the original driver
    # ------------------------------------------------------------------

    def calibrate(self, timeout: float = 30.0) -> bool:
        """Run the full motor/encoder calibration sequence."""
        from odrive.enums import AxisState as ODriveAxisState
        from odrive.utils import dump_errors, request_state

        if self._axis is None:
            return False
        self.status.state = AxisState.CALIBRATING
        request_state(self._axis, ODriveAxisState.FULL_CALIBRATION_SEQUENCE)
        start = time.time()
        time.sleep(0.5)
        while self._axis.current_state != ODriveAxisState.IDLE:
            if time.time() - start > timeout:
                return self._fail("Calibration timeout")
            time.sleep(0.1)
        if getattr(self._axis, "active_errors", 0) or getattr(self._axis, "disarm_reason", 0):
            dump_errors(self._odrive)
            return self._fail("Calibration failed - see dump_errors output")
        self.status.state = AxisState.IDLE
        return True

    def get_diagnostics(self) -> Dict:
        if self._axis is None:
            return {"connected": False}
        try:
            return {
                "connected": True,
                "axis_type": self.config.axis_type.value,
                "serial_number": self.config.serial_number,
                "state": self.status.state.value,
                "position": self.status.position,
                "torque": self.status.torque,
                "is_homed": self.status.is_homed,
                "vbus_voltage": self._odrive.vbus_voltage,
                "axis_state": self._axis.current_state,
                "errors": list(self.status.errors),
            }
        except Exception as e:
            return {"error": str(e)}

    def save_configuration(self):
        if self._odrive is not None:
            self._odrive.save_configuration()
