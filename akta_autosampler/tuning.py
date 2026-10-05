"""
Control loop tuning: step-response analysis shared by the app (Administration -> Tuning)
and tools.odrive_tune.
"""

import json
import math
from pathlib import Path
from typing import Dict, List

from .config import CONFIG_DIR

# AxisSettings field -> label, typical range (for the UI and input checks). Values are in board units
# (gain_scheduling_width in turns); the UI shows that band in mm.
GAIN_LIMITS = {
    "pos_gain": ("pos_gain  [(turns/s)/turn]", 0.0, 200.0),
    "vel_gain": ("vel_gain  [Nm/(turns/s)]", 0.0, 5.0),
    "vel_integrator_gain": ("vel_integrator_gain  [Nm/turn]", 0.0, 50.0),
    "encoder_bandwidth": ("encoder_bandwidth  [rad/s]", 50.0, 5000.0),
    # "Quiet hold": the drive lowers its gains while the axis is within the band around its target
    "enable_gain_scheduling": ("Quiet hold (gain scheduling)", 0.0, 1.0),
    "gain_scheduling_width": ("Quiet-hold band  [mm]", 0.0, 10.0),
    "gain_scheduling_min_ratio": ("Quiet-hold gain ratio  [0-1]", 0.0, 1.0),
}

PRESETS_PATH = CONFIG_DIR / "gain_presets.json"
# Presets are axis independent, so the quiet-hold band is stored in mm ("gain_scheduling_width_mm")
BUILTIN_PRESETS: Dict[str, Dict[str, float]] = {
    "ODrive default (Y)": {"pos_gain": 20.0, "vel_gain": 1 / 6, "vel_integrator_gain": 1 / 3,
                           "enable_gain_scheduling": 0.0},
    "Smooth travel + quiet hold": {"pos_gain": 20.0, "vel_gain": 0.65, "vel_integrator_gain": 0.25,
                                   "enable_gain_scheduling": 1.0, "gain_scheduling_width_mm": 1.0,
                                   "gain_scheduling_min_ratio": 0.2},
    "Z gentle": {"pos_gain": 5.0, "vel_gain": 0.05, "vel_integrator_gain": 0.1, "enable_gain_scheduling": 0.0},
}


def load_presets(path: Path = PRESETS_PATH) -> Dict[str, Dict[str, float]]:
    if not path.exists():
        save_presets(BUILTIN_PRESETS, path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_presets(presets: Dict[str, Dict[str, float]], path: Path = PRESETS_PATH) -> None:
    path.write_text(json.dumps(presets, indent=2) + "\n", encoding="utf-8")


def save_preset(name: str, values: Dict[str, float], path: Path = PRESETS_PATH) -> None:
    if not name.strip():
        raise ValueError("Preset name is empty")
    presets = load_presets(path)
    presets[name.strip()] = values
    save_presets(presets, path)


def delete_preset(name: str, path: Path = PRESETS_PATH) -> None:
    presets = load_presets(path)
    presets.pop(name, None)
    save_presets(presets, path)


def analyze_step(t: List[float], pos: List[float], t_step: float, start: float, target: float,
                 t_end: float = math.inf) -> dict:
    """Overshoot (% of step), settle time, oscillation count and steady-state error of the step
    commanded at t_step, judged on the samples up to t_end (the next command)."""
    step = target - start
    sign = 1.0 if step >= 0 else -1.0
    span = abs(step) or 1e-9
    band = max(0.05 * span, 0.002)       # settled = within 5 % (or 2 mturn)
    hyst = max(0.02 * span, 0.0007)      # ignore encoder-count noise when counting crossings
    idx = [k for k, tk in enumerate(t) if t_step <= tk < t_end]
    if not idx:
        return {"overshoot_pct": 0.0, "settle_s": math.inf, "oscillations": 0, "ss_error": step}
    err = [pos[k] - target for k in idx]
    overshoot = max(0.0, max(sign * e for e in err)) / span * 100
    settle = 0.0
    for k, e in zip(idx, err):
        if abs(e) > band:
            settle = t[k] - t_step
    if abs(err[-1]) > band:
        settle = math.inf
    crossings, side = 0, 0
    for e in err:
        s = 1 if e > hyst else -1 if e < -hyst else 0
        if s and side and s != side:
            crossings += 1
        side = s or side
    tail = [e for k, e in zip(idx, err) if t[k] >= t[idx[-1]] - 0.2]
    return {"overshoot_pct": overshoot, "settle_s": settle, "oscillations": crossings,
            "ss_error": sum(tail) / len(tail)}


def describe(name: str, step: float, m: dict) -> str:
    """One direction of a step test, e.g. 'step +0.050: reached 16 %, overshoot 0.0 %, settle never'."""
    settle = "never" if m["settle_s"] == math.inf else f"{m['settle_s'] * 1000:.0f} ms"
    return (f"{name} {step:+.3f}: reached {100 * (1 + m['ss_error'] / step):3.0f} %, "
            f"overshoot {m['overshoot_pct']:.1f} %, settle {settle}")


def rms(values: List[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0
