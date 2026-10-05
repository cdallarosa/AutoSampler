"""
Back up / restore the full ODrive configuration (every *.config.* property)
into this repo, so the boards can always be put back to a known state.

    python -m akta_autosampler.tools.odrive_backup              # read all boards -> config/odrive/<axis>_<serial>.json
    python -m akta_autosampler.tools.odrive_backup --restore    # write the saved files back + save to flash (boards reboot)

Read-only unless --restore is given. Close the ODrive web GUI / odrivetool first.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict

from ..config import AXIS_NAMES, CONFIG_DIR, load_gantry_config

BACKUP_DIR = CONFIG_DIR / "odrive"

# The settings worth seeing at a glance (the files hold everything)
SUMMARY = [
    "axis0.config.motor.motor_type", "axis0.config.motor.pole_pairs", "axis0.config.motor.torque_constant",
    "axis0.config.motor.current_soft_max", "axis0.config.motor.current_hard_max",
    "axis0.config.motor.calibration_current", "axis0.config.motor.resistance_calib_max_voltage",
    "axis0.config.motor.phase_resistance", "axis0.config.motor.phase_inductance",
    "axis0.config.motor.phase_resistance_valid", "axis0.config.motor.phase_inductance_valid",
    "axis0.config.load_encoder", "axis0.config.commutation_encoder",
    "inc_encoder0.config.enabled", "inc_encoder0.config.cpr",
    "axis0.commutation_mapper.config.use_index_gpio", "axis0.commutation_mapper.config.offset_valid",
    "axis0.config.startup_motor_calibration", "axis0.config.startup_encoder_offset_calibration",
    "axis0.config.startup_closed_loop_control", "axis0.config.startup_homing",
    "config.dc_bus_overvoltage_trip_level", "config.dc_bus_undervoltage_trip_level",
    "config.dc_max_positive_current", "config.dc_max_negative_current",
    "config.brake_resistor0.enable", "config.brake_resistor0.resistance",
    "can.config.protocol", "axis0.controller.config.vel_limit",
]


def backup_file(axis_name: str, serial: str) -> Path:
    return BACKUP_DIR / f"{axis_name}_{serial}.json"


def read_config(odrv) -> Dict[str, object]:
    from odrive.utils import backup_config
    return backup_config(odrv)


def main(argv=None):
    p = argparse.ArgumentParser(description="Back up / restore ODrive configuration")
    p.add_argument("--restore", action="store_true", help="write the saved config back to the boards")
    p.add_argument("--axis", choices=AXIS_NAMES)
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args(argv)

    from .odrive_check import find

    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name in ([args.axis] if args.axis else AXIS_NAMES):
        serial = cfg.axes[name].serial
        path = backup_file(name, serial)
        print(f"\n== {name.upper()}  ODrive {serial}")
        try:
            odrv = find(serial, args.timeout)
        except Exception as e:
            print(f"   NOT FOUND ({type(e).__name__}) - close the ODrive web GUI / odrivetool, check USB + power")
            failures += 1
            continue

        if args.restore:
            from odrive.utils import restore_config
            data = json.loads(path.read_text(encoding="utf-8"))
            errors = restore_config(odrv, data)
            for err in errors or []:
                print(f"   {err}")
            print(f"   restored {len(data)} values from {path.name}; saving to flash (board reboots)...")
            try:
                odrv.save_configuration()
            except Exception:
                pass
            time.sleep(3.0)
            continue

        data = read_config(odrv)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"   saved {len(data)} config values -> {path.relative_to(CONFIG_DIR.parent)}")
        for key in SUMMARY:
            if key in data:
                print(f"   {key:<52} {data[key]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
