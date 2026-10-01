"""
Simulated axis for running the autosampler without hardware.

Models a motor between two hard stops at motor positions 0 and
``travel_turns``. It starts mid-travel, moves at the commanded velocity
(times ``speedup``) and reports a stall current when pushed into a stop,
so the real homing algorithm in BaseAxis works unchanged.
"""

import time
from threading import Lock
from typing import Optional, Tuple

from .axis import AxisConfig, BaseAxis


class SimulatedAxis(BaseAxis):

    def __init__(self, config: AxisConfig, travel_turns: float, speedup: float = 1.0,
                 start_turns: Optional[float] = None):
        super().__init__(config)
        self.travel_turns = travel_turns
        self.speedup = speedup
        self._sim_lock = Lock()
        self._pos = travel_turns / 2 if start_turns is None else start_turns
        self._vel = 0.0
        self._mode = "idle"  # idle | position | velocity
        self._target = self._pos
        self._target_vel = 0.0
        self._cmd_vel = 0.0
        self._at_stop = False
        self._last_t = time.monotonic()
        self._injected_torque = 0.0
        self._injected_until = 0.0

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    def inject_torque(self, torque_nm: float, duration_s: float = 1.0):
        """Report ``torque_nm`` for ``duration_s`` (simulates a collision)."""
        with self._sim_lock:
            self._injected_torque = torque_nm
            self._injected_until = time.monotonic() + duration_s

    @property
    def motor_position(self) -> float:
        with self._sim_lock:
            self._integrate()
            return self._pos

    # ------------------------------------------------------------------
    # Physics
    # ------------------------------------------------------------------

    def _integrate(self):
        now = time.monotonic()
        dt = (now - self._last_t) * self.speedup
        self._last_t = now
        old = self._pos

        if self._mode == "position":
            err = self._target - self._pos
            step = self._target_vel * dt
            self._pos = self._target if abs(err) <= step else self._pos + step * (1 if err > 0 else -1)
        elif self._mode == "velocity":
            self._pos += self._cmd_vel * dt

        clamped = min(max(self._pos, 0.0), self.travel_turns)
        self._at_stop = clamped != self._pos
        self._pos = clamped
        self._vel = (self._pos - old) / dt if dt > 0 else 0.0

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    def _hw_connect(self, timeout: float) -> None:
        with self._sim_lock:
            self._last_t = time.monotonic()
            self._mode = "idle"

    def _hw_disconnect(self) -> None:
        pass

    def _hw_read(self) -> Tuple[float, float, float]:
        with self._sim_lock:
            self._integrate()
            kt = self.config.torque_constant
            current = 0.0
            if self._at_stop:
                current = 2 * self.config.homing_torque_threshold / kt
            if self._injected_torque and time.monotonic() < self._injected_until:
                current = self._injected_torque / kt
            return self._pos, self._vel, current

    def _hw_enter_closed_loop(self) -> bool:
        with self._sim_lock:
            self._integrate()
            if self._mode == "idle":
                self._mode = "position"
                self._target = self._pos
                self._target_vel = 0.0
        return True

    def _hw_set_target(self, motor_pos: float, velocity: float) -> None:
        with self._sim_lock:
            self._integrate()
            self._mode = "position"
            self._target = motor_pos
            self._target_vel = abs(velocity)

    def _hw_velocity_mode(self, velocity: float) -> None:
        with self._sim_lock:
            self._integrate()
            self._mode = "velocity"
            self._cmd_vel = velocity

    def _hw_position_mode(self) -> None:
        with self._sim_lock:
            self._integrate()
            self._mode = "position"
            self._target = self._pos
            self._cmd_vel = 0.0

    def _hw_hold(self) -> None:
        with self._sim_lock:
            self._integrate()
            if self._mode != "idle":
                self._mode = "position"
            self._target = self._pos

    def _hw_idle(self) -> None:
        with self._sim_lock:
            self._integrate()
            self._mode = "idle"
