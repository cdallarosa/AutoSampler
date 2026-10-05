"""
Tune one axis' position/velocity loop with small step responses (ODrive Pro, fw 0.6).

    python -m akta_autosampler.tools.odrive_tune x                        # read-only report
    python -m akta_autosampler.tools.odrive_tune x --step                 # step response at the board's gains
    python -m akta_autosampler.tools.odrive_tune x --step --gains 20,0.167,0.333
    ... --write-config                                                    # keep clean gains in gantry.json

The app has the same step test with a plot: Administration -> Tuning.

Gains are pos_gain, vel_gain, vel_integrator_gain. Tests change the board's RAM only and restore it
afterwards; the app applies the gains from gantry.json on every connect. The axis moves +-step turns
(default 0.05) around where it is: keep it away from the end stops. Close the app and the ODrive GUI.
"""

import argparse
import math
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import AXIS_NAMES, CONFIG_DIR, load_gantry_config, save_axis_value
from ..tuning import analyze_step, describe, rms

Gains = Tuple[float, float, float]  # pos_gain, vel_gain, vel_integrator_gain
GAIN_PATHS = ("controller.config.pos_gain", "controller.config.vel_gain", "controller.config.vel_integrator_gain")

# Test limits: a stable loop never gets near these on a 0.05 turn step
TEST_VEL_LIMIT = 2.0    # turns/s, board overspeed trips at vel_limit * vel_limit_tolerance
ABORT_VEL = 1.5         # turns/s, the tool drops to IDLE itself above this
TEST_TORQUE = 3.0       # Nm


# ----------------------------------------------------------------------
# Analysis (pure, unit tested)
# ----------------------------------------------------------------------

@dataclass
class StepResult:
    gains: Gains
    overshoot_pct: float = 0.0
    settle_s: float = math.inf
    oscillations: int = 0
    peak_vel: float = 0.0
    hold_vel_rms: float = 0.0
    ss_error: float = 0.0
    aborted: Optional[str] = None
    detail: str = ""  # per direction: how far each step got

    @property
    def ok(self) -> bool:
        return (self.aborted is None and self.oscillations <= 2 and self.overshoot_pct <= 25
                and self.settle_s <= 0.6 and self.hold_vel_rms <= 0.05)

    def line(self) -> str:
        p, v, i = self.gains
        head = f"pos {p:6.2f}  vel {v:6.4f}  int {i:6.4f} | "
        if self.aborted:
            return head + f"ABORTED: {self.aborted}"
        return head + (f"overshoot {self.overshoot_pct:5.1f} %  settle {self.settle_s * 1000:5.0f} ms  "
                       f"osc {self.oscillations}  peak vel {self.peak_vel:4.2f}  hold vel rms {self.hold_vel_rms:.3f}  "
                       f"ss err {self.ss_error * 1000:+.1f} mturn  -> {'OK' if self.ok else 'POOR'}"
                       + (f"\n        {self.detail}" if self.detail else ""))


# ----------------------------------------------------------------------
# Hardware
# ----------------------------------------------------------------------

def read_gains(axis) -> Gains:
    from ..hardware.odrive_settings import read
    return tuple(float(read(axis, p)) for p in GAIN_PATHS)  # type: ignore[return-value]


def ensure_calibrated(odrv, logger=print) -> None:
    from odrive.enums import AxisState, ProcedureResult
    from odrive.utils import request_state

    from ..hardware.odrive_axis import ODriveAxis
    axis = odrv.axis0
    if ODriveAxis._commutation_ready(axis):
        return
    logger("   encoder offset not calibrated since power-up - calibrating (the motor turns slightly)")
    odrv.clear_errors()
    request_state(axis, AxisState.ENCODER_OFFSET_CALIBRATION)
    time.sleep(0.3)
    deadline = time.time() + 30
    while axis.current_state != AxisState.IDLE:
        if time.time() > deadline:
            request_state(axis, AxisState.IDLE)
            raise RuntimeError("encoder offset calibration timed out")
        time.sleep(0.1)
    if axis.procedure_result != ProcedureResult.SUCCESS or not ODriveAxis._commutation_ready(axis):
        raise RuntimeError(f"encoder offset calibration failed: {ProcedureResult(axis.procedure_result).name}")


def step_test(odrv, gains: Gains, step: float = 0.05, hold_s: float = 0.6, settle_s: float = 1.0) -> StepResult:
    """Closed loop at the current position with these gains: hold, step +step, step back. Board RAM only."""
    from odrive.enums import AxisState, ControlMode, InputMode, ODriveError
    from odrive.utils import request_state

    from ..hardware.odrive_settings import read, write
    axis = odrv.axis0
    ctrl = axis.controller
    keep = {p: read(axis, p) for p in GAIN_PATHS + (
        "controller.config.control_mode", "controller.config.input_mode", "controller.config.vel_limit",
        "config.torque_soft_min", "config.torque_soft_max", "config.enable_watchdog", "config.watchdog_timeout")}
    res = StepResult(gains=gains)
    t: List[float] = []
    pos: List[float] = []
    vel: List[float] = []
    try:
        if axis.current_state != AxisState.IDLE:
            request_state(axis, AxisState.IDLE)
            time.sleep(0.2)
        odrv.clear_errors()
        for path, value in zip(GAIN_PATHS, gains):
            write(axis, path, value)
        ctrl.config.control_mode = ControlMode.POSITION_CONTROL
        ctrl.config.input_mode = InputMode.PASSTHROUGH   # a true step: judges the loop, not the trajectory
        ctrl.config.vel_limit = TEST_VEL_LIMIT
        axis.config.torque_soft_min, axis.config.torque_soft_max = -TEST_TORQUE, TEST_TORQUE
        axis.config.watchdog_timeout = 0.5
        p0 = axis.pos_estimate
        ctrl.input_pos = p0
        axis.watchdog_feed()
        axis.config.enable_watchdog = True
        request_state(axis, AxisState.CLOSED_LOOP_CONTROL)
        deadline = time.time() + 2
        while axis.current_state != AxisState.CLOSED_LOOP_CONTROL:
            axis.watchdog_feed()
            if time.time() > deadline:
                res.aborted = "could not enter closed loop"
                return res
            time.sleep(0.02)

        t0 = time.time()
        phases = [(hold_s, p0), (settle_s, p0 + step), (settle_s, p0)]
        t_marks = []
        for dur, target in phases:
            ctrl.input_pos = target
            t_marks.append(time.time() - t0)
            end = time.time() + dur
            while time.time() < end:
                axis.watchdog_feed()
                p, v = axis.pos_estimate, axis.vel_estimate
                t.append(time.time() - t0)
                pos.append(p)
                vel.append(v)
                if abs(v) > ABORT_VEL:
                    res.aborted = f"velocity {v:+.2f} turns/s (> {ABORT_VEL})"
                    return res
                if axis.current_state != AxisState.CLOSED_LOOP_CONTROL:
                    dr = axis.disarm_reason
                    res.aborted = "disarmed: " + (", ".join(e.name for e in ODriveError if dr & e) or str(dr))
                    return res
        hold = [v for tk, v in zip(t, vel) if 0.2 <= tk < t_marks[1]]
        res.hold_vel_rms = rms(hold)
        res.peak_vel = max(abs(v) for v in vel)
        up = analyze_step(t, pos, t_marks[1], p0, p0 + step, t_marks[2])
        back = analyze_step(t, pos, t_marks[2], p0 + step, p0)
        # Worst of the two directions (gravity / friction make them differ)
        res.overshoot_pct = max(up["overshoot_pct"], back["overshoot_pct"])
        res.settle_s = max(up["settle_s"], back["settle_s"])
        res.oscillations = max(up["oscillations"], back["oscillations"])
        res.ss_error = max(up["ss_error"], back["ss_error"], key=abs)
        res.detail = "  |  ".join(describe(name, d, m) for name, d, m in (("step", step, up), ("back", -step, back)))
        return res
    finally:
        try:
            request_state(axis, AxisState.IDLE)
        except Exception:
            pass
        time.sleep(0.1)
        for path, value in keep.items():
            try:
                write(axis, path, value)
            except Exception:
                pass
        try:
            axis.config.enable_watchdog = False
        except Exception:
            pass


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def report(name: str, odrv) -> None:
    from ..hardware.odrive_axis import ODriveAxis
    from ..hardware.odrive_settings import read
    a = odrv.axis0
    m = a.config.motor
    print(f"== {name.upper()}  ODrive {odrv.serial_number}  state {a.current_state}  errors {a.active_errors}  "
          f"disarm {a.disarm_reason}")
    print(f"   gains pos {read(a, GAIN_PATHS[0]):g}  vel {read(a, GAIN_PATHS[1]):g}  int {read(a, GAIN_PATHS[2]):g}   "
          f"encoder_bandwidth {read(a, 'config.encoder_bandwidth', '?')}  cpr {odrv.inc_encoder0.config.cpr}")
    print(f"   motor R {m.phase_resistance:.3f} ohm  L {m.phase_inductance * 1e3:.2f} mH  pole pairs {m.pole_pairs}  "
          f"encoder ready {ODriveAxis._commutation_ready(a)}  pos {a.pos_estimate:.3f} turns")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Tune an ODrive axis' position/velocity loop")
    p.add_argument("axis", choices=AXIS_NAMES)
    p.add_argument("--step", action="store_true", help="step response at --gains (default: the board's)")
    p.add_argument("--gains", help="pos,vel,int to test, e.g. 20,0.167,0.333")
    p.add_argument("--ref", choices=AXIS_NAMES, help="read the starting gains from this axis' board (read-only)")
    p.add_argument("--size", type=float, default=0.05, help="step size in turns (default 0.05)")
    p.add_argument("--write-config", action="store_true", help="save the tested/tuned gains to gantry.json")
    p.add_argument("--yes", action="store_true", help="don't ask before moving")
    args = p.parse_args(argv)

    from .odrive_check import find
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    odrv = find(cfg.axes[args.axis].serial, 10)
    report(args.axis, odrv)
    ref: Optional[Gains] = None
    if args.ref:
        ref_odrv = find(cfg.axes[args.ref].serial, 10)
        ref = read_gains(ref_odrv.axis0)
        print(f"   reference {args.ref.upper()}: pos {ref[0]:g}  vel {ref[1]:g}  int {ref[2]:g}")
    if not args.step:
        return 0

    if not args.yes and input(f"{args.axis.upper()} will move +-{args.size} turns around where it is. Type yes: ") != "yes":
        return 1
    ensure_calibrated(odrv)
    result: Optional[Gains] = None
    gains = tuple(float(v) for v in args.gains.split(",")) if args.gains else (ref or read_gains(odrv.axis0))
    r = step_test(odrv, gains, args.size)  # type: ignore[arg-type]
    print("   " + r.line())
    result = r.gains if r.ok else None
    if args.write_config:
        if result is None:
            print("not writing gantry.json: the response was not clean")
            return 1
        for key, value in zip(("pos_gain", "vel_gain", "vel_integrator_gain"), result):
            save_axis_value(args.axis, key, value, CONFIG_DIR / "gantry.json")
        print(f"saved to gantry.json ({args.axis}): pos_gain {result[0]:g}, vel_gain {result[1]:g}, "
              f"vel_integrator_gain {result[2]:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
