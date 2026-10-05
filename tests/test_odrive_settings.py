"""ODrive configuration plan / diff / write, against a fake ODrive object tree."""

from types import SimpleNamespace as NS

import pytest

from akta_autosampler.config import CONFIG_DIR, load_gantry_config
from akta_autosampler.hardware.odrive_settings import board_settings, motor_settings, read, resolve, same, write
from akta_autosampler.tools.odrive_setup import diff, planned

odrive_enums = pytest.importorskip("odrive.enums")


def fake_board():
    motor = NS(motor_type=0, pole_pairs=7, torque_constant=0.04, current_soft_max=70.0, current_hard_max=90.0,
               calibration_current=10.0, resistance_calib_max_voltage=2.0)
    axis0 = NS(config=NS(motor=motor, load_encoder=0, commutation_encoder=0), current_state=1)
    return NS(axis0=axis0,
              inc_encoder0=NS(config=NS(enabled=False, cpr=8192)),
              config=NS(dc_bus_overvoltage_trip_level=50.0, dc_bus_undervoltage_trip_level=10.5,
                        dc_max_positive_current=60.0, dc_max_negative_current=-60.0,
                        brake_resistor0=NS(enable=True, resistance=2.0)),
              can=NS(config=NS(protocol=1)))


def test_config_mirrors_board_and_leaves_unset_values_alone():
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    assert cfg.board.dc_bus_overvoltage_trip_level > 52, "48 V supply needs headroom above the bus voltage"
    paths = {s.path for s in board_settings(cfg.board)}
    assert "config.dc_bus_overvoltage_trip_level" in paths
    assert "config.dc_max_negative_current" not in paths  # null in gantry.json = leave the board's value
    assert cfg.motor.encoder_cpr == 3070 and cfg.motor.pole_pairs == 4
    assert cfg.axes["z"].encoder_cpr == 1920 and cfg.axes["z"].pole_pairs == 5  # Z: IAI 60 W motor, measured


def test_plan_diff_and_write():
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    odrv = fake_board()
    rows = diff(odrv, planned(cfg, "x"))
    changed = {s.path for s, _, ok in rows if not ok}
    assert "axis0.config.motor.pole_pairs" in changed            # 7 -> 4
    assert "config.dc_bus_overvoltage_trip_level" in changed     # 50 -> 60
    assert "config.dc_bus_undervoltage_trip_level" not in changed

    for s, _, ok in rows:
        if not ok:
            write(odrv, s.path, resolve(s))
    rows2 = diff(odrv, planned(cfg, "x"))
    assert all(ok for _, _, ok in rows2), [s.path for s, _, ok in rows2 if not ok]
    assert read(odrv, "axis0.config.motor.motor_type") == odrive_enums.MotorType.PMSM_CURRENT_CONTROL
    assert read(odrv, "axis0.config.load_encoder") == odrive_enums.EncoderId.INC_ENCODER0
    assert odrv.inc_encoder0.config.cpr == cfg.motor.encoder_cpr


def test_same_compares_enum_ints_by_name():
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    s = next(x for x in motor_settings(cfg.motor) if x.path.endswith("motor_type"))
    assert same(s, int(odrive_enums.MotorType.PMSM_CURRENT_CONTROL))
    assert not same(s, int(odrive_enums.MotorType.ACIM))


def test_brake_resistor_ohms_only_when_enabled():
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    assert not any(s.path.endswith("resistance") for s in board_settings(cfg.board))
    cfg.board.brake_resistor_enabled = True
    cfg.board.brake_resistor_ohms = 2.0
    assert any(s.path.endswith("brake_resistor0.resistance") for s in board_settings(cfg.board))


def test_encoder_offset_ready_uses_live_commutation_state():
    """Incremental encoder, no index: config.offset_valid stays False even after a good calibration."""
    from akta_autosampler.hardware.odrive_axis import ODriveAxis

    def axis(status, pos_abs, offset_valid=False):
        return NS(commutation_mapper=NS(status=status, pos_abs=pos_abs, config=NS(offset_valid=offset_valid)))
    assert ODriveAxis._commutation_ready(axis(0, 0.986))              # X after calibration (read off the board)
    assert not ODriveAxis._commutation_ready(axis(9, float("nan")))   # Y after power-up, not calibrated
    assert ODriveAxis._commutation_ready(axis(9, float("nan"), offset_valid=True))  # index / absolute encoder
