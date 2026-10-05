"""Load gantry configuration from config/gantry.json."""

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

AXIS_NAMES = ("x", "y", "z")


@dataclass
class AxisSettings:
    """Per-axis settings in mm (converted to turns via turns_per_mm)."""
    enabled: bool = True  # False = not wired yet: a simulated stand-in is used even in hardware mode
    serial: str = ""
    axis_number: int = 0
    turns_per_mm: float = 0.1
    min_mm: float = 0.0
    max_mm: float = 300.0
    speed_mm_s: float = 50.0
    accel_mm_s2: float = 100.0                 # ramp up
    decel_mm_s2: Optional[float] = None        # ramp down (None = same as accel_mm_s2)
    speed_to_home_mm_s: Optional[float] = None  # speed toward the home end (Z: up); None = speed_mm_s
    # Limits the speeds / ramps above can't exceed (None = no limit). Set in Administration -> Motion.
    max_speed_mm_s: Optional[float] = None
    max_accel_mm_s2: Optional[float] = None
    max_decel_mm_s2: Optional[float] = None
    homing_speed_mm_s: float = 10.0
    homing_direction: int = -1  # motor sense of the home hard stop (-1 or +1)
    homing_backoff_mm: float = 2.0
    encoder_cpr: Optional[int] = None  # this axis' encoder if it differs from motor.encoder_cpr
    pole_pairs: Optional[int] = None   # this axis' motor if it differs from motor.pole_pairs
    # Control loop tuning (None = leave the board's value). Written to the board on every connect.
    pos_gain: Optional[float] = None             # (turns/s) / turn
    vel_gain: Optional[float] = None             # Nm / (turns/s)
    vel_integrator_gain: Optional[float] = None  # Nm / turn
    encoder_bandwidth: Optional[float] = None    # rad/s, velocity estimate filter
    enable_gain_scheduling: Optional[float] = None    # 1 = quiet hold: lower gains near the target
    gain_scheduling_width: Optional[float] = None     # turns: the band around the target
    gain_scheduling_min_ratio: Optional[float] = None  # 0-1: how far the gains drop inside the band


# AxisSettings field -> ODrive property (relative to axisN)
LOOP_SETTINGS = {
    "pos_gain": "controller.config.pos_gain",
    "vel_gain": "controller.config.vel_gain",
    "vel_integrator_gain": "controller.config.vel_integrator_gain",
    "encoder_bandwidth": "config.encoder_bandwidth",
    "enable_gain_scheduling": "controller.config.enable_gain_scheduling",
    "gain_scheduling_width": "controller.config.gain_scheduling_width",
    "gain_scheduling_min_ratio": "controller.config.gain_scheduling_min_ratio",
}
BOOL_LOOP_SETTINGS = {"enable_gain_scheduling"}


@dataclass
class MotorSettings:
    """Motor parameters shared by all axes."""
    # False (default): use the motor/encoder/board setup already stored on the ODrive
    # (e.g. from the ODrive web GUI). True: write the values below on every connect.
    write_motor_config: bool = False
    motor_type: str = "PMSM_CURRENT_CONTROL"
    pole_pairs: int = 4
    torque_constant: float = 0.095
    encoder_cpr: int = 3070
    current_soft_max: float = 30.0
    current_hard_max: float = 49.0
    calibration_current: float = 10.0
    torque_soft_limit: float = 2.0
    torque_hard_limit: float = 4.0
    homing_torque_threshold: float = 1.5
    resistance_calib_max_voltage: float = 6.0


@dataclass
class BoardSettings:
    """Board-level ODrive settings (DC bus, brake resistor, CAN) for tools.odrive_setup. None = leave as is."""
    dc_bus_overvoltage_trip_level: Optional[float] = None
    dc_bus_undervoltage_trip_level: Optional[float] = None
    dc_max_positive_current: Optional[float] = None
    dc_max_negative_current: Optional[float] = None
    brake_resistor_enabled: Optional[bool] = None
    brake_resistor_ohms: Optional[float] = None
    can_protocol: Optional[str] = None


@dataclass
class SimulationSettings:
    travel_mm: Dict[str, float] = field(default_factory=lambda: {"x": 320.0, "y": 320.0, "z": 160.0})
    speedup: float = 4.0


@dataclass
class GantryConfig:
    simulate: bool = True
    axes: Dict[str, AxisSettings] = field(
        default_factory=lambda: {name: AxisSettings() for name in AXIS_NAMES})
    motor: MotorSettings = field(default_factory=MotorSettings)
    board: BoardSettings = field(default_factory=BoardSettings)
    simulation: SimulationSettings = field(default_factory=SimulationSettings)
    z_safe_mm: float = 0.0  # Z height (from top) that clears all labware
    home_order: List[str] = field(default_factory=lambda: ["z", "x", "y"])
    # "sensorless" = Home drives into the hard stops. "manual" = put the gantry at the home corner
    # (jog or by hand) and click Set home here; Home then only checks that this was done.
    homing_mode: str = "sensorless"
    # Speed setting for every move (jog, Go, runs), % of each axis' speed_mm_s / accel_mm_s2.
    # The app starts with this value; the operator can change it in System Control.
    speed_percent: float = 100.0
    position_tolerance_mm: float = 0.1  # fine tolerance (homing back-off, 'already there' checks)
    # A move counts as done once the axis is this close to its target and has slowed to a stop: the needle
    # goes into wide bottles, so the drive need not fight for the last fraction of a millimetre.
    arrive_tolerance_mm: float = 2.0
    move_timeout_s: float = 60.0
    homing_timeout_s: float = 120.0
    watchdog_timeout_s: float = 0.5


def _build(cls, data: dict):
    data = {k: v for k, v in data.items() if not k.startswith("_")}  # "_comment" keys allowed anywhere
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def gantry_config_from_dict(data: dict) -> GantryConfig:
    data = {k: v for k, v in data.items() if not k.startswith("_")}  # allow "_comment" keys
    axes = data.pop("axes", {})
    missing = set(AXIS_NAMES) - set(axes)
    if missing:
        raise ValueError(f"gantry config missing axes: {sorted(missing)}")
    cfg = _build(GantryConfig, {
        **data,
        "axes": {name: _build(AxisSettings, axes[name]) for name in AXIS_NAMES},
        "motor": _build(MotorSettings, data.get("motor", {})),
        "board": _build(BoardSettings, data.get("board", {})),
        "simulation": _build(SimulationSettings, data.get("simulation", {})),
    })
    for name, ax in cfg.axes.items():
        if ax.homing_direction not in (-1, 1):
            raise ValueError(f"axes.{name}.homing_direction must be -1 or 1")
        if ax.turns_per_mm <= 0:
            raise ValueError(f"axes.{name}.turns_per_mm must be > 0")
    return cfg


def save_turns_per_mm(name: str, value: float, path: Path = CONFIG_DIR / "gantry.json") -> None:
    """Write one axis' turns_per_mm into gantry.json (only that number changes; layout and comments stay)."""
    save_axis_value(name, "turns_per_mm", value, path)


def save_axis_value(name: str, key: str, value: float, path: Path = CONFIG_DIR / "gantry.json") -> None:
    """Write one number of one axis into gantry.json (only that number changes; layout and comments stay)."""
    text = path.read_text(encoding="utf-8")
    block = re.search(rf'"{re.escape(name)}"\s*:\s*\{{[^{{}}]*\}}', text)
    if not block:
        raise ValueError(f"axis '{name}' not found in {path.name}")
    new_block, n = re.subn(rf'("{re.escape(key)}"\s*:\s*)[-+0-9.eE]+', lambda mt: f"{mt.group(1)}{value:.6g}",
                           block.group(0))
    if n == 0:  # new key: append it as the axis' last entry
        body = block.group(0)
        head = body[:body.rfind("}")].rstrip()  # everything up to the last value
        indent = re.search(r'\n([ \t]*)"', body)
        new_block = f'{head},\n{indent.group(1) if indent else "      "}"{key}": {value:.6g}{body[len(head):]}'
    elif n != 1:
        raise ValueError(f"{key} found more than once for axis '{name}' in {path.name}")
    path.write_text(text[:block.start()] + new_block + text[block.end():], encoding="utf-8")


def load_gantry_config(path: Path = CONFIG_DIR / "gantry.json") -> GantryConfig:
    with open(path, encoding="utf-8") as f:
        return gantry_config_from_dict(json.load(f))
