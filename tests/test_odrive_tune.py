"""Step-response analysis used by tools.odrive_tune (no hardware)."""
import math

import pytest

from akta_autosampler.tools.odrive_tune import StepResult, analyze_step, rms


def trace(kind, step=0.05, dt=0.002, t_step=0.5, dur=1.5):
    t = [k * dt for k in range(int(dur / dt))]
    pos = []
    for tk in t:
        if tk < t_step:
            pos.append(0.0)
            continue
        x = tk - t_step
        if kind == "clean":          # critically damped
            pos.append(step * (1 - (1 + 30 * x) * math.exp(-30 * x)))
        elif kind == "ringing":      # under-damped, oscillates a lot
            pos.append(step * (1 - math.exp(-3 * x) * math.cos(60 * x)))
        elif kind == "sluggish":     # never gets there
            pos.append(step * (1 - math.exp(-1 * x)))
    return t, pos


def test_clean_step_is_fast_and_flat():
    t, pos = trace("clean")
    m = analyze_step(t, pos, 0.5, 0.0, 0.05)
    assert m["overshoot_pct"] < 1 and m["oscillations"] == 0 and m["settle_s"] < 0.3
    assert abs(m["ss_error"]) < 0.001


def test_ringing_step_counts_oscillations_and_overshoot():
    t, pos = trace("ringing")
    m = analyze_step(t, pos, 0.5, 0.0, 0.05)
    assert m["overshoot_pct"] > 50 and m["oscillations"] >= 4


def test_sluggish_step_never_settles():
    t, pos = trace("sluggish")
    m = analyze_step(t, pos, 0.5, 0.0, 0.05)
    assert m["settle_s"] == math.inf and m["ss_error"] < -0.01


def test_downward_step_and_result_verdicts():
    t, pos = trace("clean", step=-0.05)
    assert analyze_step(t, pos, 0.5, 0.0, -0.05)["overshoot_pct"] < 1
    good = StepResult(gains=(20, 0.167, 0.333), overshoot_pct=3, settle_s=0.1, oscillations=0, hold_vel_rms=0.01)
    assert good.ok and "OK" in good.line()
    assert not StepResult(gains=(1, 0.2, 0), aborted="disarmed: VELOCITY_LIMIT_VIOLATION").ok
    assert rms([3, -4]) == pytest.approx(math.sqrt(12.5))


def test_out_move_is_judged_only_until_the_move_back():
    t, pos = trace("clean", t_step=0.5, dur=1.5)
    t2 = [tk + 1.5 for tk in t]
    back = [0.05 - p for p in pos]  # then return to 0
    m = analyze_step(t + t2, pos + back, 0.5, 0.0, 0.05, t_end=1.5)
    assert abs(m["ss_error"]) < 0.001 and m["settle_s"] < 0.3


def test_presets_round_trip(tmp_path):
    from akta_autosampler.tuning import BUILTIN_PRESETS, delete_preset, load_presets, save_preset
    p = tmp_path / "presets.json"
    assert set(load_presets(p)) == set(BUILTIN_PRESETS)          # created with the built-ins
    save_preset("Mine", {"pos_gain": 12.0, "enable_gain_scheduling": 1.0, "gain_scheduling_width_mm": 0.5}, p)
    assert load_presets(p)["Mine"]["gain_scheduling_width_mm"] == 0.5
    delete_preset("Mine", p)
    assert "Mine" not in load_presets(p)
    with pytest.raises(ValueError):
        save_preset("  ", {}, p)


def test_quiet_hold_settings_apply_and_save(tmp_path):
    from akta_autosampler.config import CONFIG_DIR, load_gantry_config
    from akta_autosampler.gantry import Gantry
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    cfg.simulate = True
    g = Gantry(cfg)
    assert g.connect()
    p = tmp_path / "gantry.json"
    p.write_text((CONFIG_DIR / "gantry.json").read_text(encoding="utf-8"), encoding="utf-8")
    g.apply_tuning("x", {"enable_gain_scheduling": 1.0, "gain_scheduling_width": 0.05, "gain_scheduling_min_ratio": 0.2},
                   save_to=p)
    assert g.read_tuning("x")["enable_gain_scheduling"] == 1.0
    assert g.x_axis.config.loop_settings["controller.config.gain_scheduling_width"] == 0.05
    saved = load_gantry_config(p).axes["x"]
    assert (saved.enable_gain_scheduling, saved.gain_scheduling_min_ratio) == (1, 0.2)
    with pytest.raises(ValueError):
        g.apply_tuning("x", {"gain_scheduling_min_ratio": 2.0})
    g.disconnect()
