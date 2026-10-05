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
    assert homed.move_to(x=250, speed=20)  # slow enough to still be moving when the "collision" hits
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

        def spy(position, velocity=None, accel=None, decel=None, _n=name, _orig=original):
            calls.append(_n)
            return _orig(position, velocity, accel, decel)
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
    t = threading.Thread(target=lambda: result.setdefault("ok", homed.move_to(x=570, speed=10, wait=True)))
    t.start()
    time.sleep(0.2)
    homed.stop()
    t.join(5)
    assert result["ok"] is False
    assert homed.get_position().x < 569
    assert homed.move_to(x=10, wait=True)  # a normal stop keeps homing


def test_emergency_stop_requires_rehome(homed):
    homed.stop(emergency=True)
    assert not homed.is_homed
    assert not homed.move_to(x=10)


def test_xy_vector_move_is_straight_and_simultaneous(homed):
    """Both axes move the whole way: the path is a straight diagonal and they arrive together."""
    import threading as _t
    assert homed.move_to(x=50, y=50, wait=True)
    start, end = (50.0, 50.0), (550.0, 450.0)
    track = []
    done = _t.Event()

    kx, ky = homed.config.axes["x"].turns_per_mm, homed.config.axes["y"].turns_per_mm
    ax, ay = homed.x_axis, homed.y_axis

    def sample():
        # Read both simulated motors back-to-back (the status cache can lag ~10 ms per axis)
        while not done.is_set():
            x = ax._from_motor(ax.motor_position) / kx
            y = ay._from_motor(ay.motor_position) / ky
            track.append((time.time(), x, y))
            time.sleep(0.002)
    th = _t.Thread(target=sample, daemon=True)
    th.start()
    assert homed.safe_move_to(*end)
    done.set()
    th.join()

    (x0, y0), (x1, y1) = start, end
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    # perpendicular distance of every sample from the straight line
    worst = max(abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / length for _, x, y in track)
    assert worst < 3.0, f"path deviates {worst:.1f} mm from a straight line"
    # both axes finish at (almost) the same moment
    t_x = next(t for t, x, _ in track if abs(x - x1) < 0.5)
    t_y = next(t for t, _, y in track if abs(y - y1) < 0.5)
    assert abs(t_x - t_y) < 0.05


def test_home_runs_startup_calibration_first(gantry):
    calls = []
    for name, axis in gantry.axes.items():
        orig = axis.prepare

        def spy(e_stop=None, _n=name, _orig=orig):
            calls.append(_n)
            return _orig(e_stop)
        axis.prepare = spy
    assert gantry.home_all()
    assert calls == ["z", "x", "y"]          # every axis prepared, in homing order
    assert all(a.is_prepared for a in gantry.axes.values())
    calls.clear()
    assert gantry.home_all()
    assert calls == []                       # already prepared: not repeated


def test_failed_startup_calibration_blocks_homing(gantry):
    gantry.z_axis.prepare = lambda e_stop=None: False
    assert not gantry.home_all()
    assert not gantry.is_homed
    assert not gantry.x_axis.status.is_homed  # stopped before homing anything else


def test_calibration_api_in_simulation(gantry):
    status = gantry.calibration_status()
    assert set(status) == {"x", "y", "z"} and all(s["motor"] for s in status.values())
    assert gantry.calibrate("y", "encoder_offset")
    assert gantry.y_axis.is_prepared
    assert gantry.save_to_board("y")
    with pytest.raises(ValueError):
        gantry.calibrate("x", "bogus")


def test_disabled_axis_gets_a_stand_in_in_hardware_mode(config):
    from akta_autosampler.gantry import Gantry
    from akta_autosampler.hardware.sim_axis import SimulatedAxis
    config.simulate = False
    for ax in config.axes.values():
        ax.enabled = False  # nothing wired: every axis is a stand-in, no ODrive needed
    g = Gantry(config)
    assert all(isinstance(a, SimulatedAxis) for a in g.axes.values())
    assert g.connect()
    assert all(s["stand_in"] for s in g.calibration_status().values())
    g.disconnect()


def test_rescale_keeps_homing_and_moves_true_mm(homed):
    from akta_autosampler.gantry import Gantry
    k_true = homed.config.axes["x"].turns_per_mm      # the simulator's real mechanics
    assert homed.jog("x", 50, wait=True)
    # pretend the configured scale was 2x too small: a "50 mm" move really went 100 mm
    k_new = Gantry.rescaled_turns_per_mm(k_true, 50, 100)
    assert k_new == pytest.approx(k_true / 2)
    turns = homed.x_axis.motor_position
    homed.set_turns_per_mm("x", k_new)
    assert homed.is_homed and homed.x_axis.motor_position == pytest.approx(turns)
    assert homed.get_position().x == pytest.approx(100, abs=0.5)   # same spot, read with the new scale
    assert homed.move_to(x=200, wait=True)
    assert homed.get_position().x == pytest.approx(200, abs=0.5)
    assert homed.x_axis.config.position_max == pytest.approx(homed.config.axes["x"].max_mm * k_new)
    with pytest.raises(ValueError):
        Gantry.rescaled_turns_per_mm(0.1, 50, 1)  # 50x off: a typo, not a measurement


def test_save_turns_per_mm_changes_only_that_value(tmp_path):
    from akta_autosampler.config import CONFIG_DIR, load_gantry_config, save_turns_per_mm
    src = (CONFIG_DIR / "gantry.json").read_text(encoding="utf-8")
    p = tmp_path / "gantry.json"
    p.write_text(src, encoding="utf-8")
    save_turns_per_mm("y", 0.25, p)
    cfg = load_gantry_config(p)
    assert cfg.axes["y"].turns_per_mm == 0.25
    assert cfg.axes["x"].turns_per_mm == load_gantry_config(CONFIG_DIR / "gantry.json").axes["x"].turns_per_mm
    changed = [a for a, b in zip(src.splitlines(), p.read_text(encoding="utf-8").splitlines()) if a != b]
    assert len(changed) == 1 and '"turns_per_mm": 0.25' not in changed[0]


def test_manual_homing_sets_zero_where_the_gantry_is(gantry):
    gantry.config.homing_mode = "manual"
    assert not gantry.home_all()                    # nothing set yet: Home refuses instead of driving
    assert not gantry.home_axis("x")
    assert gantry.jog("x", 30) and gantry.jog("y", 20)
    turns = gantry.x_axis.motor_position
    assert gantry.set_home_here()
    assert gantry.is_homed and gantry.home_all()     # Home is now a no-op
    pos = gantry.get_position()
    assert (pos.x, pos.y) == pytest.approx((0, 0), abs=0.2)
    assert gantry.move_to(x=100, wait=True)
    assert gantry.x_axis.motor_position == pytest.approx(turns + 100 * gantry.config.axes["x"].turns_per_mm, abs=0.02)
    assert not gantry.move_to(x=-5)                  # soft limits apply from the new home


def test_moves_blocked_until_axis_is_calibrated(gantry):
    gantry.x_axis.motion_blocker = lambda: "X: encoder offset calibration needed"
    assert not gantry.jog("x", 5)
    gantry.x_axis.motion_blocker = lambda: None
    assert gantry.jog("x", 5)


def test_prepare_all_calibrates_every_axis(gantry):
    assert not any(a.is_prepared for a in gantry.axes.values())
    assert gantry.prepare_all()
    assert all(a.is_prepared for a in gantry.axes.values())


def test_speed_setting_slows_every_move_and_keeps_paths_straight(homed):
    homed.set_speed_percent(25)
    seen = {}
    for name, axis in homed.axes.items():
        orig = axis.move_to_position

        def spy(position, velocity=None, accel=None, decel=None, _n=name, _orig=orig):
            seen[_n] = (velocity, accel)
            return _orig(position, velocity, accel, decel)
        axis.move_to_position = spy
    assert homed.move_to(x=40, wait=True)
    k = homed.config.axes["x"].turns_per_mm
    assert seen["x"][0] == pytest.approx(homed.config.axes["x"].speed_mm_s * 0.25 * k)
    seen.clear()
    assert homed.move_linear(type(homed.get_position())(140, 90, None), wait=True)
    kx, ky = homed.config.axes["x"].turns_per_mm, homed.config.axes["y"].turns_per_mm
    (vx, ax), (vy, ay) = seen["x"], seen["y"]  # motor turns: compare in mm
    assert (vx / kx) / (vy / ky) == pytest.approx(100 / 90) and (ax / kx) / (ay / ky) == pytest.approx(100 / 90)
    homed.set_speed_percent(500)
    assert homed.speed_percent == 100  # clamped


def test_slow_move_gets_a_longer_timeout(homed):
    homed.set_speed_percent(5)
    homed.config.move_timeout_s = 0.5
    homed.x_axis.config.move_timeout = 0.5
    assert homed.move_to(x=60, wait=True)  # takes longer than 0.5 s at 5 %; must not time out


def test_find_limits_measures_both_stops_and_saves(gantry, tmp_path):
    from akta_autosampler.config import CONFIG_DIR, load_gantry_config
    p = tmp_path / "gantry.json"
    p.write_text((CONFIG_DIR / "gantry.json").read_text(encoding="utf-8"), encoding="utf-8")
    found = gantry.find_limits(["z", "x", "y"], save_to=p)
    cfg = gantry.config
    for n in ("x", "y"):
        # simulator: hard stops at 0 and travel_mm; a back-off at each end
        expected = cfg.simulation.travel_mm[n] - 2 * cfg.axes[n].homing_backoff_mm
        assert found[n] == pytest.approx(expected, abs=0.6)
        assert cfg.axes[n].max_mm == found[n] == load_gantry_config(p).axes[n].max_mm
    assert gantry.is_homed
    pos = gantry.get_position()
    assert (pos.x, pos.y) == pytest.approx((0, 0), abs=0.3)    # back home
    assert not gantry.move_to(x=found["x"] + 1)                 # new soft limit enforced
    assert gantry.move_to(x=found["x"] - 1, wait=True)


def test_tuning_apply_save_and_step_test(homed, tmp_path):
    from akta_autosampler.config import CONFIG_DIR, load_gantry_config
    p = tmp_path / "gantry.json"
    p.write_text((CONFIG_DIR / "gantry.json").read_text(encoding="utf-8"), encoding="utf-8")
    homed.apply_tuning("x", {"pos_gain": 12.0, "vel_gain": 0.3, "vel_integrator_gain": 0.6}, save_to=p)
    assert homed.read_tuning("x")["vel_gain"] == 0.3
    assert homed.x_axis.config.loop_settings["controller.config.pos_gain"] == 12.0
    assert load_gantry_config(p).axes["x"].vel_integrator_gain == 0.6
    with pytest.raises(ValueError):
        homed.apply_tuning("x", {"vel_gain": 99})
    r = homed.step_test("x", 2.0)
    assert r["aborted"] is None and r["metrics"]["error_mm"] < 0.05 and r["metrics"]["oscillations"] == 0
    assert max(r["pos_mm"]) == pytest.approx(2.0, abs=0.05)       # went out 2 mm (away from home) ...
    assert r["pos_mm"][-1] == pytest.approx(0.0, abs=0.05)        # ... and came back
    assert homed.get_position().x == pytest.approx(0.0, abs=0.1)


def test_step_test_goes_the_way_there_is_room(homed):
    assert homed.move_to(x=homed.config.axes["x"].max_mm - 1, wait=True)
    r = homed.step_test("x", 2.0)
    assert r["size_mm"] == -2.0 and min(r["pos_mm"]) == pytest.approx(-2.0, abs=0.05)


def test_find_limits_on_one_axis_returns_it_home(gantry):
    found = gantry.find_limits(["x"])
    assert found and "x" in found and not gantry.is_homed          # Y / Z untouched
    assert gantry.x_axis.status.is_homed
    assert gantry.get_position().x == pytest.approx(0, abs=0.3)


def test_home_all_moves_z_first_then_x_and_y_together(gantry):
    import threading as _t
    spans = {}
    lock = _t.Lock()
    for name, axis in gantry.axes.items():
        orig = axis.home

        def spy(e_stop=None, _n=name, _orig=orig):
            t0 = time.time()
            ok = _orig(e_stop)
            with lock:
                spans[_n] = (t0, time.time())
            return ok
        axis.home = spy
    assert gantry.home_all()
    assert spans["z"][1] <= min(spans["x"][0], spans["y"][0])           # Z done before X / Y start
    assert spans["x"][0] < spans["y"][1] and spans["y"][0] < spans["x"][1]  # X and Y overlap in time


def test_failed_axis_stops_the_other_one(gantry):
    gantry.y_axis.home = lambda e_stop=None: False
    assert not gantry.home_all()
    assert not gantry.is_homed and not gantry.y_axis.status.is_homed


def test_find_limits_raises_z_before_xy_and_measures_all(gantry):
    found = gantry.find_limits(["x", "y", "z"])
    cfg = gantry.config
    assert set(found) == {"x", "y", "z"}
    assert found["z"] == pytest.approx(cfg.simulation.travel_mm["z"] - 2 * cfg.axes["z"].homing_backoff_mm, abs=0.6)
    pos = gantry.get_position()
    assert (pos.x, pos.y, pos.z) == pytest.approx((0, 0, 0), abs=0.3)  # everything back home, needle up


def test_direction_speeds_and_separate_ramps(homed, tmp_path):
    from akta_autosampler.config import CONFIG_DIR, load_gantry_config
    p = tmp_path / "gantry.json"
    p.write_text((CONFIG_DIR / "gantry.json").read_text(encoding="utf-8"), encoding="utf-8")
    homed.set_motion("z", speed_mm_s=10, accel_mm_s2=300, decel_mm_s2=300, speed_to_home_mm_s=20, save_to=p)
    homed.set_motion("x", speed_mm_s=80, accel_mm_s2=90, decel_mm_s2=60, save_to=p)
    seen = {}
    for name, axis in homed.axes.items():
        orig = axis.move_to_position

        def spy(position, velocity=None, accel=None, decel=None, _n=name, _orig=orig):
            seen[_n] = (velocity, accel, decel)
            return _orig(position, velocity, accel, decel)
        axis.move_to_position = spy
    kz, kx = homed.config.axes["z"].turns_per_mm, homed.config.axes["x"].turns_per_mm
    assert homed.move_to(z=40, wait=True)            # down: speed_mm_s
    assert seen["z"][0] == pytest.approx(10 * kz)
    assert homed.move_to(z=5, wait=True)             # up (toward home): speed_to_home_mm_s, faster
    assert seen["z"][0] == pytest.approx(20 * kz)
    assert homed.move_to(x=100, wait=True)
    assert seen["x"][1:] == pytest.approx((90 * kx, 60 * kx))   # ramp up 90, ramp down 60
    cfg = load_gantry_config(p)
    assert (cfg.axes["z"].speed_to_home_mm_s, cfg.axes["x"].decel_mm_s2) == (20, 60)
    with pytest.raises(ValueError):
        homed.set_motion("x", speed_mm_s=-1, accel_mm_s2=100)


def test_move_is_done_within_arrive_tolerance_once_stopped(homed):
    ax = homed.x_axis
    k = homed.config.axes["x"].turns_per_mm
    ax.config.arrive_tolerance = 2.0 * k  # the app default (tests run with a fine 0.1 mm)
    ax.status.target_position = ax.status.position + 1.5 * k      # 1.5 mm short of the target ...
    ax.status.velocity = 0.0
    assert ax.is_at_position()                                     # ... but stopped: done (2 mm tolerance)
    ax.status.velocity = 1.0                                       # still sliding in: not done yet
    assert not ax.is_at_position()
    ax.status.velocity = 0.0
    ax.status.target_position = ax.status.position + 3.0 * k      # too far
    assert not ax.is_at_position()
    ax.status.target_position = None


def test_reload_settings_applies_without_restart(homed, tmp_path):
    from akta_autosampler.config import CONFIG_DIR, save_axis_value
    p = tmp_path / "gantry.json"
    p.write_text((CONFIG_DIR / "gantry.json").read_text(encoding="utf-8"), encoding="utf-8")
    save_axis_value("x", "speed_mm_s", 55, p)
    save_axis_value("x", "decel_mm_s2", 77, p)
    changed = homed.reload_settings(p)
    assert any(c.startswith("X:") and "speed_mm_s" in c and "decel_mm_s2" in c for c in changed)
    k = homed.config.axes["x"].turns_per_mm
    prof = homed.x_axis.config.motion_profile
    assert prof.deceleration_limit == pytest.approx(77 * k)
    assert homed.read_motion("x")["decel_limit"] == pytest.approx(77)
    assert homed.is_homed                                   # homing survives a reload


def test_step_test_uses_real_speed_and_returns_speed_trace(homed):
    homed.set_speed_percent(50)
    r = homed.step_test("x", 20.0)
    assert r["aborted"] is None
    expect = homed.config.axes["x"].speed_mm_s * 0.5
    assert r["settings"]["speed_out"] == pytest.approx(expect)
    assert len(r["vel_mm_s"]) == len(r["t"])
    peak = max(abs(v) for v in r["vel_mm_s"])
    assert 0.5 * expect <= peak <= 1.1 * expect  # cruises at the set speed, never above it (sim samples coarsely)


def test_speed_and_ramp_limits(homed, tmp_path):
    from akta_autosampler.config import CONFIG_DIR, load_gantry_config
    p = tmp_path / "gantry.json"
    p.write_text((CONFIG_DIR / "gantry.json").read_text(encoding="utf-8"), encoding="utf-8")
    homed.set_limits("x", 80, 100, 100, save_to=p)
    with pytest.raises(ValueError):                       # above the limit: refused
        homed.set_motion("x", speed_mm_s=80, accel_mm_s2=150)
    homed.set_motion("x", speed_mm_s=80, accel_mm_s2=100, decel_mm_s2=60, save_to=p)
    homed.config.axes["x"].accel_mm_s2 = 400              # edited by hand past the limit ...
    seen = {}
    orig = homed.x_axis.move_to_position

    def spy(position, velocity=None, accel=None, decel=None):
        seen["a"] = accel
        return orig(position, velocity, accel, decel)
    homed.x_axis.move_to_position = spy
    assert homed.move_to(x=50, wait=True)
    assert seen["a"] == pytest.approx(100 * homed.config.axes["x"].turns_per_mm)   # ... moves still capped
    homed.set_limits("x", 60, 100, 100, save_to=p)        # lowering a limit lowers the setting
    assert homed.config.axes["x"].speed_mm_s == 60 and load_gantry_config(p).axes["x"].speed_mm_s == 60
