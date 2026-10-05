"""
Configure the ODrive Pro boards from config/gantry.json ("motor" + "board").

    python -m akta_autosampler.tools.odrive_setup            # dry run: current vs proposed, nothing written
    python -m akta_autosampler.tools.odrive_setup --apply    # write, save to flash (board reboots), verify
    python -m akta_autosampler.tools.odrive_setup --axis z   # only one axis' board

Calibration is NOT run here - it needs the motor and encoder connected.
Close the ODrive web GUI / odrivetool first (only one program can hold the USB link).
"""

import argparse
import dataclasses
import sys
import time
from typing import Dict, List, Tuple

from ..config import AXIS_NAMES, CONFIG_DIR, GantryConfig, load_gantry_config
from ..hardware.odrive_settings import (_MISSING, Setting, board_settings, display, motor_settings, read, resolve,
                                        same, write)


def planned(cfg: GantryConfig, axis_name: str) -> List[Setting]:
    ax = cfg.axes[axis_name]
    overrides = {k: getattr(ax, k) for k in ("encoder_cpr", "pole_pairs") if getattr(ax, k) is not None}
    motor = dataclasses.replace(cfg.motor, **overrides)
    return board_settings(cfg.board) + motor_settings(motor, ax.axis_number)


def diff(odrv, settings: List[Setting]) -> List[Tuple[Setting, object, bool]]:
    return [(s, read(odrv, s.path, _MISSING), same(s, read(odrv, s.path, _MISSING))) for s in settings]


def print_diff(serial: str, label: str, rows) -> int:
    changes = sum(1 for _, _, ok in rows if not ok)
    print(f"\n== ODrive {serial}  ({label})  - {changes} change{'s' if changes != 1 else ''}")
    print(f"   {'setting':<48} {'current':>22}    {'proposed':<22}")
    for s, cur, ok in rows:
        mark = "  " if ok else "->"
        print(f" {mark} {s.path:<48} {display(s, cur):>22}    {display(s, s.value) if not s.enum else s.value:<22}"
              f"  {s.note}")
    return changes


def apply(odrv, rows, serial: str, timeout: float):
    from akta_autosampler.tools.odrive_check import find

    axis_states = [read(odrv, f"axis{n}.current_state", 1) for n in (0, 1)]
    if any(st not in (1, None) for st in axis_states if st is not _MISSING):  # 1 = IDLE
        raise RuntimeError(f"ODrive {serial}: axis is not IDLE - refusing to reconfigure")
    for s, _, ok in rows:
        if not ok:
            write(odrv, s.path, resolve(s))
    print(f"   wrote {sum(1 for _, _, ok in rows if not ok)} settings; saving to flash (the board reboots)...")
    try:
        odrv.save_configuration()
    except Exception:
        pass  # the USB link drops while the board reboots
    time.sleep(3.0)
    odrv2 = find(serial, timeout)
    after = [(s, read(odrv2, s.path, _MISSING), same(s, read(odrv2, s.path, _MISSING))) for s, _, _ in rows]
    bad = [s.path for s, _, ok in after if not ok]
    if bad:
        raise RuntimeError(f"ODrive {serial}: these did not stick after reboot: {bad}")
    print(f"   verified after reboot: all {len(after)} settings match")


def main(argv=None):
    p = argparse.ArgumentParser(description="Configure ODrive boards from config/gantry.json")
    p.add_argument("--apply", action="store_true", help="write + save (default is a dry run)")
    p.add_argument("--axis", choices=AXIS_NAMES, help="only this axis' board")
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args(argv)

    from akta_autosampler.tools.odrive_check import find

    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    boards: Dict[str, List[str]] = {}
    for name in ([args.axis] if args.axis else AXIS_NAMES):
        boards.setdefault(cfg.axes[name].serial, []).append(name)

    total, failures = 0, 0
    for serial, names in boards.items():
        label = ", ".join(f"{n.upper()}=axis{cfg.axes[n].axis_number}" for n in names)
        try:
            odrv = find(serial, args.timeout)
        except Exception as e:
            print(f"\n== ODrive {serial} ({label}): NOT FOUND ({type(e).__name__}). "
                  "Close the ODrive web GUI / odrivetool and check USB + power.")
            failures += 1
            continue
        settings: List[Setting] = []
        for n in names:  # board settings once per board, motor settings per axis
            for s in planned(cfg, n):
                if s.path not in {x.path for x in settings}:
                    settings.append(s)
        rows = diff(odrv, settings)
        total += print_diff(serial, label, rows)
        if args.apply and any(not ok for _, _, ok in rows):
            try:
                apply(odrv, rows, serial, args.timeout)
            except Exception as e:
                failures += 1
                print(f"   FAILED: {e}")

    if not args.apply:
        print(f"\nDry run - nothing written. {total} change(s) proposed. Re-run with --apply to write and save.")
    print("Next: connect motors + encoders, then calibrate each axis (ODrive web GUI or odrivetool: "
          "request FULL_CALIBRATION_SEQUENCE).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
