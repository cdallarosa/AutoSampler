"""
ODrive axis driver.

Ported from ProteinMakerV5 system_configuration/odrive/odrive_class.py.
``odrive`` is imported lazily so simulation mode works without the package
(or the WinUSB driver) installed.

Targets the odrive 0.6.x Python package (ODrive Pro / S1 firmware); a few
attribute fallbacks for older firmware are kept from the original.
"""

import math
import time
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple

from .axis import AxisConfig, AxisState, BaseAxis
from ..config import BOOL_LOOP_SETTINGS, LOOP_SETTINGS
from .odrive_settings import Setting, read, resolve, write


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
    don't each run find_any(). If ``board_setup`` is given (motor.write_motor_config
    in gantry.json), those board-level settings are written once per board.
    """

    def __init__(self, board_setup: Optional[List[Setting]] = None):
        self.board_setup = board_setup
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
                for st in self.board_setup or []:
                    write(board, st.path, resolve(st))
                self._configured.add(serial)
            return board

    def release(self, serial: str):
        with self._lock:
            self._boards.pop(serial, None)
            self._configured.discard(serial)


class ODriveAxis(BaseAxis):
    """One ODrive axis, identified by board serial number + axis0/axis1."""

    def __init__(self, config: AxisConfig, pool: Optional[ODriveBoardPool] = None,
                 motor_setup: Optional[List[Setting]] = None):
        super().__init__(config)
        self._pool = pool or ODriveBoardPool()
        self._motor_setup = motor_setup  # written on connect only if motor.write_motor_config is true
        self._odrive = None
        self._axis = None

    # ------------------------------------------------------------------
    # Connection / configuration
    # ------------------------------------------------------------------

    def _hw_connect(self, timeout: float) -> None:
        self._odrive = self._pool.get(self.config.serial_number, timeout)
        if self.config.axis_number not in (0, 1):
            raise ValueError(f"Invalid axis number: {self.config.axis_number}")
        axis = getattr(self._odrive, f"axis{self.config.axis_number}", None)
        if axis is None:
            raise ValueError(f"ODrive {self.config.serial_number} has no axis{self.config.axis_number} "
                             f"(ODrive Pro / S1 boards have a single axis: use axis_number 0)")
        self._axis = axis
        self._configure_axis()
        # Calibrated earlier in this power cycle (e.g. in the ODrive GUI)? Then it is ready to move.
        cal = self.calibration_status()
        self._prepared = bool(cal["motor"] and cal["encoder_offset"])
        self.logger.info("Calibration: ready" if self._prepared else
                         "Calibration: encoder offset not calibrated since power-up - click Calibrate")

    def _hw_disconnect(self) -> None:
        if self._axis is not None:
            try:  # nobody feeds the watchdog after we leave - switch it off so it can't trip
                self._axis.config.enable_watchdog = False
            except Exception as e:
                self.logger.warning(f"Could not disable watchdog: {e}")
        self._pool.release(self.config.serial_number)
        self._odrive = None
        self._axis = None

    def _configure_axis(self):
        self.logger.info("Configuring axis...")
        if self._motor_setup:
            for st in self._motor_setup:
                write(self._odrive, st.path, resolve(st))
            self.logger.info("Wrote motor/encoder config from gantry.json")
        else:
            self.logger.info("Using the motor/encoder setup stored on the ODrive (write_motor_config is off)")
        self._configure_motion()

    def _configure_motion(self):
        """Settings the autosampler always needs: position mode, trajectory, torque clamp, watchdog."""
        from odrive.enums import ControlMode, InputMode

        cfg = self.config
        axis = self._axis
        # Position control with trapezoidal trajectory
        axis.controller.config.control_mode = ControlMode.POSITION_CONTROL
        axis.controller.config.input_mode = InputMode.TRAP_TRAJ
        profile = cfg.motion_profile
        axis.controller.config.vel_limit = max(profile.velocity_limit, cfg.homing_velocity) * 1.2
        axis.controller.config.vel_limit_tolerance = 1.2
        axis.trap_traj.config.vel_limit = profile.velocity_limit
        axis.trap_traj.config.accel_limit = profile.acceleration_limit
        axis.trap_traj.config.decel_limit = profile.deceleration_limit

        # Tuned control loop gains from gantry.json (anything not set keeps the board's value)
        for path, value in cfg.loop_settings.items():
            write(axis, path, bool(value) if path.endswith(tuple(BOOL_LOOP_SETTINGS)) else float(value))
        if cfg.loop_settings:
            self.logger.info("Loop tuning: " + ", ".join(f"{p.split('.')[-1]}={v:g}" for p, v in cfg.loop_settings.items()))

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
        # A stale error (e.g. the watchdog after the app was closed) would block arming. Real faults
        # are tracked by this app (sticky) and never reach here: moves on a faulted axis are refused.
        if read(self._axis, "active_errors", 0):
            self.logger.info(f"Clearing board errors before enabling: {self._axis.active_errors}")
            self._hw_clear_errors()
        # Arm exactly where we are. In TRAP_TRAJ mode the trajectory starts from the controller's
        # last setpoint, which is stale after the axis was idle (an unfinished move, the ODrive GUI,
        # pushing by hand): the axis would lurch toward it on arming. PASSTHROUGH pins the setpoint
        # to input_pos, so arm in PASSTHROUGH, then switch back to the trajectory mode.
        from odrive.enums import InputMode
        ctrl = self._axis.controller
        mode = ctrl.config.input_mode
        here = self._read_pos_vel()[0]
        ctrl.config.input_mode = InputMode.PASSTHROUGH
        ctrl.input_vel = 0
        ctrl.input_torque = 0
        ctrl.input_pos = here
        try:
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
        finally:
            ctrl.input_pos = here
            ctrl.config.input_mode = mode
            ctrl.input_pos = here  # new trajectory starts from the pinned setpoint: no motion

    def _hw_set_target(self, motor_pos: float, velocity: float, accel: Optional[float] = None,
                       decel: Optional[float] = None) -> None:
        profile = self.config.motion_profile
        traj = self._axis.trap_traj.config
        traj.vel_limit = velocity
        # Ramp up / ramp down; vector moves scale both per axis so the path stays a straight line
        traj.accel_limit = accel if accel is not None else profile.acceleration_limit
        traj.decel_limit = (decel if decel is not None else
                            accel if accel is not None else profile.deceleration_limit)
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
    # Calibration / start-up procedure
    # ------------------------------------------------------------------

    @staticmethod
    def _commutation_ready(ax) -> bool:
        """
        Encoder offset usable right now. With an incremental encoder and no index the offset is only
        kept for this power cycle: config.offset_valid stays False (it is the stored-offset flag for
        index / absolute encoders). The live commutation mapper shows it instead: status 0 and a real
        pos_abs once calibrated (before: status != 0, pos_abs NaN).
        """
        if read(ax, "commutation_mapper.config.offset_valid", False):
            return True
        status = read(ax, "commutation_mapper.status", None)
        pos_abs = read(ax, "commutation_mapper.pos_abs", None)
        return status == 0 and isinstance(pos_abs, (int, float)) and math.isfinite(pos_abs)

    def calibration_status(self) -> Dict[str, object]:
        ax = self._axis
        if ax is None:
            return {"connected": False, "simulated": False, "motor": False, "encoder_offset": False,
                    "resistance": None, "inductance": None, "prepared": False}
        return {
            "connected": True,
            "simulated": False,
            "motor": bool(read(ax, "config.motor.phase_resistance_valid", True))
            and bool(read(ax, "config.motor.phase_inductance_valid", True)),
            "encoder_offset": self._commutation_ready(ax),
            "resistance": read(ax, "config.motor.phase_resistance", None),
            "inductance": read(ax, "config.motor.phase_inductance", None),
            "prepared": self._prepared,
        }

    def motion_blocker(self) -> Optional[str]:
        if self._prepared:
            return None
        cal = self.calibration_status()
        if cal["motor"] and cal["encoder_offset"]:
            self._prepared = True  # already calibrated on the board (e.g. reconnect without power cycle)
            return None
        what = "encoder offset" if cal["motor"] else "motor + encoder offset (Full)"
        return (f"{self.config.axis_type.value}: {what} calibration needed before it can move - "
                f"click Calibrate (or Administration -> Calibration)")

    def prepare(self, e_stop=None, timeout: float = 60.0) -> bool:
        """
        Power-up procedure for an incremental encoder without index:
          - motor not calibrated (R/L invalid) -> full calibration
          - otherwise, encoder offset not valid -> encoder offset calibration
        """
        if self._axis is None:
            return self._fail("Not connected")
        cal = self.calibration_status()
        if cal["motor"] and cal["encoder_offset"]:
            self._prepared = True
            return True
        return self.run_calibration("encoder_offset" if cal["motor"] else "full", e_stop, timeout)

    def run_calibration(self, kind: str, e_stop=None, timeout: float = 60.0) -> bool:
        """
        kind: "motor" (measures R/L - beeps, does not turn), "encoder_offset"
        (turns the motor slightly) or "full" (both). Stops on e-stop, timeout
        or any ODrive procedure error. The axis is left IDLE (motor unpowered).
        """
        from odrive.enums import AxisState as S
        from odrive.enums import ProcedureResult

        states = {"motor": S.MOTOR_CALIBRATION, "encoder_offset": S.ENCODER_OFFSET_CALIBRATION,
                  "full": S.FULL_CALIBRATION_SEQUENCE}
        if kind not in states:
            raise ValueError(f"kind must be one of {list(states)}")
        ax = self._axis
        if ax is None:
            return self._fail("Not connected")
        target = states[kind]
        label = {"motor": "motor calibration", "encoder_offset": "encoder offset calibration",
                 "full": "full calibration"}[kind]
        self.logger.info(f"Running {label}" + ("" if kind == "motor" else " (the motor will turn slightly)"))
        self.status.state = AxisState.CALIBRATING
        self.status.target_position = None
        try:
            if int(ax.current_state) != int(S.IDLE):  # leave closed loop first
                ax.requested_state = S.IDLE
                time.sleep(0.2)
            self._hw_clear_errors()
            ax.watchdog_feed()
            ax.requested_state = target
            time.sleep(0.3)
            deadline = time.time() + timeout
            while int(ax.current_state) != int(S.IDLE):
                if e_stop and e_stop():
                    ax.requested_state = S.IDLE
                    return self._fail(f"{label.capitalize()} stopped")
                if time.time() > deadline:
                    ax.requested_state = S.IDLE
                    return self._fail(f"{label.capitalize()} timed out after {timeout:.0f} s")
                time.sleep(0.1)
            result = read(ax, "procedure_result", None)
            if result is not None and int(result) != int(ProcedureResult.SUCCESS):
                try:
                    name = ProcedureResult(int(result)).name
                except ValueError:
                    name = str(result)
                return self._fail(f"{label.capitalize()} failed: {name} (active_errors={read(ax, 'active_errors', '?')}, "
                                  f"disarm_reason={read(ax, 'disarm_reason', '?')})")
            cal = self.calibration_status()
            if kind in ("motor", "full") and not cal["motor"]:
                return self._fail(f"{label.capitalize()} finished but the motor is still not calibrated")
            if kind in ("encoder_offset", "full"):
                if not cal["encoder_offset"]:
                    return self._fail(f"{label.capitalize()} finished but the encoder offset is still not valid")
                self._prepared = True
            self.status.state = AxisState.IDLE
            self.logger.info(f"{label.capitalize()} complete"
                             + (f" - R={cal['resistance']:.4f} ohm, L={cal['inductance'] * 1e3:.3f} mH"
                                if kind in ("motor", "full") and cal["resistance"] is not None else ""))
            return True
        except Exception as e:
            try:
                ax.requested_state = S.IDLE
            except Exception:
                pass
            return self._fail(f"{label.capitalize()} error: {e}")

    def read_tuning(self) -> Dict[str, float]:
        return {key: float(read(self._axis, path)) for key, path in LOOP_SETTINGS.items()}

    def apply_tuning(self, values: Dict[str, float]) -> None:
        with self._lock:
            for key, value in values.items():
                write(self._axis, LOOP_SETTINGS[key], bool(value) if key in BOOL_LOOP_SETTINGS else float(value))
        self.logger.info("Tuning applied: " + ", ".join(f"{k}={v:g}" for k, v in values.items()))

    def read_motion(self) -> Dict[str, float]:
        """The trajectory limits the drive is using now (set by the last move) and its overspeed limit."""
        t = self._axis.trap_traj.config
        return {"vel_limit": float(t.vel_limit), "accel_limit": float(t.accel_limit),
                "decel_limit": float(t.decel_limit), "controller_vel_limit": float(self._axis.controller.config.vel_limit)}

    def _hw_armed(self) -> bool:
        from odrive.enums import AxisState as ODriveAxisState
        return self._axis.current_state == ODriveAxisState.CLOSED_LOOP_CONTROL

    def _hw_disarm_text(self) -> str:
        from odrive.enums import ODriveError
        dr = read(self._axis, "disarm_reason", 0)
        return ", ".join(e.name for e in ODriveError if dr & e) or f"disarm_reason {dr}"

    def apply_motion_config(self) -> None:
        if self.is_connected and self._axis is not None:
            with self._lock:
                self._configure_motion()

    def save_to_board(self, reconnect_timeout: float = 15.0) -> bool:
        """
        Save the board's configuration (incl. motor calibration) to flash.
        The ODrive reboots, so we disconnect, save, wait and reconnect. The
        encoder offset and homing are lost by the reboot.
        """
        odrv = self._odrive
        if odrv is None:
            return self._fail("Not connected")
        self.logger.info("Saving configuration to the board (it reboots)...")
        self.disconnect()
        try:
            odrv.save_configuration()
        except Exception:
            pass  # the USB link drops while the board reboots
        time.sleep(4.0)
        if not self.connect(timeout=reconnect_timeout):
            return self._fail("Board did not come back after saving - check USB / power")
        self.logger.info("Configuration saved; board reconnected (start-up calibration needed again)")
        return True

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
