"""
Simulated ÄKTA for running handshakes without hardware.

Models a method that loops over samples:

    Equilibration -> request sample (handshake request signal = active)
    -> wait for needle-ready -> Sample Application for ``sample_time_s``
    -> request signal back to idle -> wait for needle-ready to clear -> repeat

Every configured signal is served from one in-memory table, so the same
config/akta.json works in sim and hardware mode.
"""

import threading
import time
from typing import Any, Dict, Optional

from .backends import Backend


class SimAkta(Backend):
    name = "sim"

    def __init__(self, handshake: dict, run_state_signal: Optional[str] = "run_state",
                 phase_signal: Optional[str] = "phase", equilibrate_s: float = 2.0,
                 sample_time_s: float = 3.0):
        super().__init__()
        self.values: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.equilibrate_s = equilibrate_s
        self.sample_time_s = sample_time_s
        self.run_state_signal = run_state_signal
        self.phase_signal = phase_signal

        req, ready = handshake["request"], handshake["ready"]
        self.req_signal, self.req_active = req["signal"], req["equals"]
        self.req_idle = handshake["done"]["equals"]
        self.ready_signal = ready["signal"]
        self.ready_active, self.ready_idle = ready["active"], ready["idle"]
        self.samples_done = 0

    # -- Backend ---------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            self.values[self.req_signal] = self.req_idle
            self.values[self.ready_signal] = self.ready_idle
            self._set_status("Running", "Equilibration")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sim-akta")
        self._thread.start()
        self.connected = True
        self.error = None

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.connected = False

    def read(self, spec: dict) -> Any:
        with self._lock:
            return self.values.get(spec["_name"])

    def read_many(self, specs: Dict[str, dict]) -> Dict[str, Any]:
        with self._lock:
            return {n: self.values.get(n) for n in specs}

    def write(self, spec: dict, value: Any) -> None:
        with self._lock:
            self.values[spec["_name"]] = value

    # -- Simulated method ------------------------------------------------

    def _set_status(self, state: str, phase: str):
        if self.run_state_signal:
            self.values[self.run_state_signal] = state
        if self.phase_signal:
            self.values[self.phase_signal] = phase

    def _get(self, name):
        with self._lock:
            return self.values.get(name)

    def _wait(self, predicate) -> bool:
        while not self._stop.is_set():
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _run(self):
        while not self._stop.is_set():
            if self._stop.wait(self.equilibrate_s):
                return
            with self._lock:
                self._set_status("Hold", "Sample Application - waiting for autosampler")
                self.values[self.req_signal] = self.req_active
            if not self._wait(lambda: self._get(self.ready_signal) == self.ready_active):
                return
            with self._lock:
                self._set_status("Running", "Sample Application")
            if self._stop.wait(self.sample_time_s):
                return
            with self._lock:
                self.values[self.req_signal] = self.req_idle
                self._set_status("Running", "Column Wash")
                self.samples_done += 1
            if not self._wait(lambda: self._get(self.ready_signal) == self.ready_idle):
                return
            with self._lock:
                self._set_status("Running", "Equilibration")
