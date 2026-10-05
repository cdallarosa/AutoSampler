"""
Read-only ODrive USB check - finds the boards and reports what it sees.
Nothing is written to the boards and no motor is moved.

    python -m akta_autosampler.tools.odrive_check            # boards listed in config/gantry.json
    python -m akta_autosampler.tools.odrive_check --any      # first ODrive found on USB (shows its serial)

Close odrivetool / the ODrive web GUI first: only one program can hold an
ODrive's USB connection at a time.
"""

import argparse
import sys
from typing import Optional

from ..config import CONFIG_DIR, load_gantry_config


def _get(obj, path: str, default=None):
    """Read a dotted attribute path, tolerating firmware differences."""
    try:
        for part in path.split("."):
            obj = getattr(obj, part)
        return obj
    except Exception:
        return default


def _serial_str(odrv) -> str:
    sn = _get(odrv, "serial_number")
    return f"{sn:012X}" if isinstance(sn, int) else str(sn)


def describe(odrv, axes=(0, 1)) -> str:
    from odrive.enums import AxisState
    lines = [
        f"  serial      : {_serial_str(odrv)}",
        f"  hardware    : v{_get(odrv, 'hw_version_major', '?')}.{_get(odrv, 'hw_version_minor', '?')}"
        f" (variant {_get(odrv, 'hw_version_variant', '?')})",
        f"  firmware    : {_get(odrv, 'fw_version_major', '?')}.{_get(odrv, 'fw_version_minor', '?')}."
        f"{_get(odrv, 'fw_version_revision', '?')}",
        f"  bus voltage : {_get(odrv, 'vbus_voltage', float('nan')):.1f} V",
    ]
    for n in axes:
        axis = _get(odrv, f"axis{n}")
        if axis is None:
            continue
        state = _get(axis, "current_state")
        try:
            state = AxisState(state).name
        except Exception:
            pass
        errors = _get(axis, "active_errors", _get(axis, "error", "?"))
        disarm = _get(axis, "disarm_reason", "-")
        pos = _get(axis, "pos_vel_mapper.pos_rel", _get(axis, "encoder.pos_estimate", "?"))
        calibrated = _get(axis, "motor.is_calibrated", _get(axis, "config.motor.phase_resistance", "?"))
        lines.append(f"  axis{n}       : state={state}  active_errors={errors}  disarm_reason={disarm}  "
                     f"pos={pos if isinstance(pos, str) else f'{pos:.3f}'} turns  calibrated/R={calibrated}")
    return "\n".join(lines)


def find(serial: Optional[str], timeout: float):
    import odrive
    if serial:
        return odrive.find_any(serial_number=serial, timeout=timeout)
    return odrive.find_any(timeout=timeout)


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only ODrive USB check")
    p.add_argument("--any", action="store_true", help="report the first ODrive found, whatever its serial")
    p.add_argument("--timeout", type=float, default=8.0)
    args = p.parse_args(argv)

    import importlib.util
    if importlib.util.find_spec("odrive") is None:
        print("The 'odrive' package is not installed:  pip install odrive")
        return 2

    if args.any:
        targets = {None: "any"}
    else:
        cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
        targets = {}
        for name, ax in cfg.axes.items():
            targets.setdefault(ax.serial, []).append(f"{name.upper()}=axis{ax.axis_number}")

    ok = True
    for serial, used_by in targets.items():
        label = "any ODrive" if serial is None else f"ODrive {serial} ({', '.join(used_by)})"
        print(f"Looking for {label} on USB (timeout {args.timeout:.0f} s)...")
        try:
            odrv = find(serial, args.timeout)
        except Exception as e:  # timeout or USB error
            ok = False
            print(f"  NOT FOUND: {type(e).__name__}: {e}")
            print("  -> check USB cable + power, close odrivetool/web GUI, and that the serial in "
                  "config/gantry.json matches (use --any to read it).")
            continue
        print(describe(odrv))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
