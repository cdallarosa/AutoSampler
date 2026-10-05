"""
The ODrive Pro configuration this machine needs, as a list of (path, value)
settings - one source of truth for tools.odrive_setup and the optional
write-on-connect in odrive_axis.

Paths are relative to the ODrive object, e.g. "axis0.config.motor.pole_pairs".
Enum values are given as names ("PMSM_CURRENT_CONTROL", "INC_ENCODER0", "NONE")
and resolved against odrive.enums when written, so this module imports
nothing from odrive at import time.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from ..config import BoardSettings, MotorSettings

_ENUM_FIELDS = {
    "motor_type": "MotorType",
    "load_encoder": "EncoderId",
    "commutation_encoder": "EncoderId",
    "protocol": "Protocol",
}


@dataclass
class Setting:
    path: str
    value: Any
    note: str = ""

    @property
    def enum(self) -> Optional[str]:
        return _ENUM_FIELDS.get(self.path.rsplit(".", 1)[-1])


def motor_settings(m: MotorSettings, axis_number: int = 0) -> List[Setting]:
    a = f"axis{axis_number}"
    return [
        Setting(f"{a}.config.motor.motor_type", m.motor_type, "motor type"),
        Setting(f"{a}.config.motor.pole_pairs", m.pole_pairs, "pole pairs"),
        Setting(f"{a}.config.motor.torque_constant", m.torque_constant, "Nm/A (8.27 / KV)"),
        Setting(f"{a}.config.motor.current_soft_max", m.current_soft_max, "A"),
        Setting(f"{a}.config.motor.current_hard_max", m.current_hard_max, "A, trips above this"),
        Setting(f"{a}.config.motor.calibration_current", m.calibration_current, "A during calibration"),
        Setting(f"{a}.config.motor.resistance_calib_max_voltage", m.resistance_calib_max_voltage, "V"),
        Setting("inc_encoder0.config.enabled", True, "incremental encoder input on"),
        Setting("inc_encoder0.config.cpr", m.encoder_cpr, "counts per rev (4 x PPR)"),
        Setting(f"{a}.config.load_encoder", "INC_ENCODER0", "position feedback"),
        Setting(f"{a}.config.commutation_encoder", "INC_ENCODER0", "commutation feedback"),
    ]


def board_settings(b: BoardSettings) -> List[Setting]:
    """Only the board settings that are set (None = leave whatever is on the board)."""
    out = [
        Setting("config.dc_bus_overvoltage_trip_level", b.dc_bus_overvoltage_trip_level, "V"),
        Setting("config.dc_bus_undervoltage_trip_level", b.dc_bus_undervoltage_trip_level, "V"),
        Setting("config.dc_max_positive_current", b.dc_max_positive_current, "A from the supply"),
        Setting("config.dc_max_negative_current", b.dc_max_negative_current, "A back into the supply (regen)"),
        Setting("config.brake_resistor0.enable", b.brake_resistor_enabled, "brake resistor"),
    ]
    if b.brake_resistor_enabled:
        out.append(Setting("config.brake_resistor0.resistance", b.brake_resistor_ohms, "ohm"))
    out.append(Setting("can.config.protocol", b.can_protocol, "CAN (NONE = off)"))
    return [s for s in out if s.value is not None]


# ----------------------------------------------------------------------------
# read / write helpers (work on any object tree; the odrive enums are optional)
# ----------------------------------------------------------------------------

_MISSING = object()


def read(obj, path: str, default=_MISSING):
    try:
        for part in path.split("."):
            obj = getattr(obj, part)
        return obj
    except AttributeError:
        if default is _MISSING:
            raise
        return default


def write(obj, path: str, value) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        obj = getattr(obj, part)
    setattr(obj, leaf, value)


def resolve(setting: Setting):
    """The value to write: enum names -> odrive.enums members."""
    if setting.enum and isinstance(setting.value, str):
        import odrive.enums as enums
        enum_cls = getattr(enums, setting.enum)
        try:
            return enum_cls[setting.value]
        except KeyError:
            raise ValueError(f"{setting.path}: unknown {setting.enum} '{setting.value}'. "
                             f"Options: {[e.name for e in enum_cls]}") from None
    return setting.value


def display(setting: Setting, value) -> str:
    """Human-readable current value (enum ints shown by name when odrive is available)."""
    if value is _MISSING or value is None:
        return "<n/a>"
    if setting.enum:
        try:
            import odrive.enums as enums
            return getattr(enums, setting.enum)(value).name
        except Exception:
            pass
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def same(setting: Setting, current) -> bool:
    if current is _MISSING:
        return False
    wanted = setting.value
    if setting.enum:
        return display(setting, current) == (wanted if isinstance(wanted, str) else display(setting, wanted))
    if isinstance(wanted, bool) or isinstance(current, bool):
        return bool(current) == bool(wanted)
    if isinstance(wanted, (int, float)) and isinstance(current, (int, float)):
        return abs(float(current) - float(wanted)) <= 1e-6 * max(1.0, abs(float(wanted)))
    return current == wanted
