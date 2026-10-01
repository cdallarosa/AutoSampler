import threading
import time

import pytest

from akta_autosampler.hardware.axis import AxisState


def test_homing_sets_corner_origin(homed):
    pos = homed.get_position()
    assert pos.x == pytest.approx(0, abs=0.2)
    assert pos.y == pytest.approx(0, abs=0.2)
    assert pos.z == pytest.approx(0, abs=0.2)
    cfg = homed.config
    # X/Y home at the low hard stop (motor 0); Z homes up at the high stop
    x_backoff = cfg.axes["x"].homing_backoff_mm * cfg.axes["x"].turns_per_mm
    assert homed.x_axis.motor_position == pytest.approx(x_backoff, abs=0.02)
    z_travel = cfg.simulation.travel_mm["z"] * cfg.axes["z"].turns_per_mm
    z_backoff = cfg.axes["z"].homing_backoff_mm * cfg.axes["z"].turns_per_mm
    assert homed.z_axis.motor_position == pytest.approx(z_travel - z_backoff, abs=0.02)


def test_positive_coordinates_move_away_from_home(homed):
    assert homed.move_to(x=100, z=50, wait=True)
    pos = homed.get_position()
    assert pos.x == pytest.approx(100, abs=0.2)
    assert pos.z == pytest.approx(50, abs=0.2)
    # Z homed at the high motor end, so moving down decreases motor turns
    assert homed.z_axis.motor_position < homed.z_axis._offset


def test_out_of_limit_move_moves_nothing(homed):
    assert not homed.move_to(x=100, y=999, wait=True)
    assert homed.get_position().x == pytest.approx(0, abs=0.2)
    assert all(a.status.state != AxisState.MOVING for a in homed.axes.values())


def test_absolute_moves_require_homing(gantry):
    assert not gantry.move_to(x=10)
    start = gantry.get_position().x
    assert gantry.jog("x", 5)
    assert gantry.get_position().x == pytest.approx(start + 5, abs=0.2)


def test_torque_fault_is_sticky(homed):
    assert homed.move_to(x=250)
    time.sleep(0.05)
    homed.x_axis.inject_torque(5.0, duration_s=0.5)
    assert not homed.wait_for_moves(timeout=5)
    assert homed.x_axis.status.state == AxisState.TORQUE_LIMIT_REACHED
    time.sleep(0.2)  # the monitor thread must not clear the fault
    assert homed.x_axis.status.state == AxisState.TORQUE_LIMIT_REACHED
    assert not homed.move_to(x=10)
    time.sleep(0.5)
    homed.reset_all()
    assert homed.move_to(x=10, wait=True)


def test_safe_move_order(homed):
    assert homed.move_to(x=50, y=50, z=60, wait=True)
    calls = []
    for name, axis in homed.axes.items():
        original = axis.move_to_position

        def spy(position, velocity=None, _n=name, _orig=original):
            calls.append(_n)
            return _orig(position, velocity)
        axis.move_to_position = spy

    assert homed.safe_move_to(150, 120, 40)
    assert calls == ["z", "x", "y", "z"]
    pos = homed.get_position()
    assert (pos.x, pos.y, pos.z) == pytest.approx((150, 120, 40), abs=0.2)


def test_safe_move_skips_raise_when_xy_unchanged(homed):
    assert homed.move_to(x=50, y=50, z=20, wait=True)
    assert homed.safe_move_to(50, 50, 60)
    assert homed.get_position().z == pytest.approx(60, abs=0.2)


def test_stop_aborts_blocking_move(homed):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", homed.move_to(x=290, wait=True)))
    t.start()
    time.sleep(0.1)
    homed.stop()
    t.join(5)
    assert result["ok"] is False
    assert homed.get_position().x < 289
    assert homed.move_to(x=10, wait=True)  # a normal stop keeps homing


def test_emergency_stop_requires_rehome(homed):
    homed.stop(emergency=True)
    assert not homed.is_homed
    assert not homed.move_to(x=10)
