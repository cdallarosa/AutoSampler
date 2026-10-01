"""Load gantry configuration from config/gantry.json."""

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

AXIS_NAMES = ("x", "y", "z")


@dataclass
class AxisSettings:
    """Per-axis settings in mm (converted to turns via turns_per_mm)."""
    serial: str = ""
    axis_number: int = 0
    turns_per_mm: float = 0.1
    min_mm: float = 0.0
    max_mm: float = 300.0
    speed_mm_s: float = 50.0
    accel_mm_s2: float = 100.0
    homing_speed_mm_s: float = 10.0
    homing_direction: int = -1  # motor sense of the home hard stop (-1 or +1)
    homing_backoff_mm: float = 2.0


@dataclass
class MotorSettings:
    """Motor parameters shared by all axes."""
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
    simulation: SimulationSettings = field(default_factory=SimulationSettings)
    z_safe_mm: float = 0.0  # Z height (from top) that clears all labware
    home_order: List[str] = field(default_factory=lambda: ["z", "x", "y"])
    position_tolerance_mm: float = 0.1
    move_timeout_s: float = 60.0
    homing_timeout_s: float = 120.0
    watchdog_timeout_s: float = 0.5


def _build(cls, data: dict):
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
        "simulation": _build(SimulationSettings, data.get("simulation", {})),
    })
    for name, ax in cfg.axes.items():
        if ax.homing_direction not in (-1, 1):
            raise ValueError(f"axes.{name}.homing_direction must be -1 or 1")
        if ax.turns_per_mm <= 0:
            raise ValueError(f"axes.{name}.turns_per_mm must be > 0")
    return cfg


def load_gantry_config(path: Path = CONFIG_DIR / "gantry.json") -> GantryConfig:
    with open(path, encoding="utf-8") as f:
        return gantry_config_from_dict(json.load(f))
