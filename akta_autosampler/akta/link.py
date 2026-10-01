"""
AktaLink: named ÄKTA signals over OPC UA and/or LabJack digital I/O.

config/akta.json defines *signals*. Each one is read from (or written to)
one backend::

    "run_state":      {"source": "opcua",   "node": "ns=2;s=..."}
    "sample_request": {"source": "labjack", "line": "FIO4"}               # ÄKTA Digital out 1
    "needle_ready":   {"source": "labjack", "line": "FIO5", "output": true, "idle": 1}  # ÄKTA Digital in 1

A background thread polls every input signal; sequences wait on the cached
values and write outputs through ``write``.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import CONFIG_DIR
from .backends import Backend, make_backends
from .sim import SimAkta

logger = logging.getLogger(__name__)

SOURCES = ("opcua", "labjack")


class AktaError(RuntimeError):
    pass


def load_akta_config(path: Path = CONFIG_DIR / "akta.json") -> dict:
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    for name, spec in config.get("signals", {}).items():
        if name.startswith("_"):
            continue
        src = spec.get("source")
        if src not in SOURCES:
            raise ValueError(f"signal '{name}': source must be one of {SOURCES}")
        if src == "opcua" and not spec.get("node"):
            raise ValueError(f"signal '{name}': opcua signals need 'node'")
        if src == "labjack" and not spec.get("line"):
            raise ValueError(f"signal '{name}': labjack signals need 'line'")
    hs = config.get("handshake")
    if hs:
        for key in ("request", "ready", "done"):
            if hs[key]["signal"] not in config["signals"]:
                raise ValueError(f"handshake.{key} uses unknown signal '{hs[key]['signal']}'")
    return config


def matches(value: Any, condition: Dict[str, Any]) -> bool:
    """Check a signal value against {"equals"|"not_equals"|"in"|"contains": ...}."""
    if value is None:
        return False

    def eq(a, b):
        if isinstance(a, str) or isinstance(b, str):
            return str(a).strip().lower() == str(b).strip().lower()
        return float(a) == float(b)

    if "equals" in condition:
        return eq(value, condition["equals"])
    if "not_equals" in condition:
        return not eq(value, condition["not_equals"])
    if "in" in condition:
        return any(eq(value, c) for c in condition["in"])
    if "contains" in condition:
        return str(condition["contains"]).lower() in str(value).lower()
    raise ValueError(f"Condition needs equals/not_equals/in/contains: {condition}")


def describe_condition(condition: Dict[str, Any]) -> str:
    for key, sym in (("equals", "="), ("not_equals", "≠"), ("in", "in"), ("contains", "contains")):
        if key in condition:
            return f"{sym} {condition[key]}"
    return str(condition)


class AktaLink:

    def __init__(self, config: dict, simulate: bool = False):
        self.config = config
        self.simulated = simulate
        self.signals: Dict[str, dict] = {
            n: {**s, "_name": n} for n, s in config.get("signals", {}).items() if not n.startswith("_")
        }
        self.handshake: Optional[dict] = config.get("handshake")
        self.poll_s = config.get("poll_s", 0.2)
        self.reconnect_s = config.get("reconnect_s", 5.0)

        if simulate:
            if not self.handshake:
                raise ValueError("Simulation needs a 'handshake' section in akta.json")
            sim = SimAkta(self.handshake, **config.get("simulation", {}))
            self.backends: Dict[str, Backend] = {"sim": sim}
            self._route = {n: "sim" for n in self.signals}
        else:
            self.backends = {b.name: b for b in make_backends(config)}
            self._route = {n: s["source"] for n, s in self.signals.items()}
            missing = {src for src in self._route.values()} - set(self.backends)
            if missing:
                raise ValueError(f"Signals use {sorted(missing)} but it is not configured in akta.json")

        self._values: Dict[str, Any] = {}
        self._stamps: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_attempt: Dict[str, float] = {}
        self.running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect all backends (failures are retried by the poll thread)."""
        for backend in self.backends.values():
            self._try_connect(backend)
        if not self.running:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="akta-link")
            self._thread.start()
            self.running = True
        if all(b.connected for b in self.backends.values()):
            self.reset_outputs()
        return all(b.connected for b in self.backends.values())

    def disconnect(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.running = False
        for backend in self.backends.values():
            backend.disconnect()

    def _try_connect(self, backend: Backend):
        self._last_attempt[backend.name] = time.time()
        try:
            backend.connect()
        except Exception as e:
            backend.connected = False
            backend.error = f"{type(e).__name__}: {e}"
            logger.error(f"ÄKTA {backend.name} connect failed: {backend.error}")

    def _poll_loop(self):
        while not self._stop.is_set():
            for name, backend in self.backends.items():
                if not backend.connected:
                    if time.time() - self._last_attempt.get(name, 0) > self.reconnect_s:
                        self._try_connect(backend)
                    continue
                specs = {n: self.signals[n] for n, b in self._route.items() if b == name}
                try:
                    values = backend.read_many(specs)
                except Exception as e:
                    backend.error = f"{type(e).__name__}: {e}"
                    logger.error(f"ÄKTA {name} read failed: {backend.error}")
                    backend.disconnect()
                    continue
                now = time.time()
                with self._lock:
                    for n, v in values.items():
                        self._values[n] = v
                        self._stamps[n] = now
            self._stop.wait(self.poll_s)

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _spec(self, name: str) -> dict:
        try:
            return self.signals[name]
        except KeyError:
            raise AktaError(f"Unknown ÄKTA signal '{name}'. Signals: {list(self.signals)}") from None

    def get(self, name: str) -> Any:
        self._spec(name)
        with self._lock:
            return self._values.get(name)

    def write(self, name: str, value: Any):
        spec = self._spec(name)
        if not spec.get("output"):
            raise AktaError(f"Signal '{name}' is not an output")
        backend = self.backends[self._route[name]]
        if not backend.connected:
            raise AktaError(f"ÄKTA {backend.name} not connected")
        backend.write(spec, value)
        with self._lock:
            self._values[name] = value
            self._stamps[name] = time.time()
        logger.info(f"ÄKTA signal {name} := {value}")

    def pulse(self, name: str, active: Any, seconds: float, cancel: Optional[threading.Event] = None):
        idle = self._spec(name).get("idle")
        if idle is None:
            raise AktaError(f"Signal '{name}' needs an 'idle' value to pulse")
        self.write(name, active)
        try:
            if cancel is not None:
                cancel.wait(seconds)
            else:
                time.sleep(seconds)
        finally:
            self.write(name, idle)

    def reset_outputs(self):
        """Put every output with an 'idle' value back to idle (best effort)."""
        for name, spec in self.signals.items():
            if spec.get("output") and "idle" in spec:
                try:
                    self.write(name, spec["idle"])
                except Exception as e:
                    logger.warning(f"Could not reset ÄKTA output {name}: {e}")

    def wait_for(self, name: str, condition: Dict[str, Any], timeout: Optional[float] = None,
                 cancel: Optional[threading.Event] = None) -> bool:
        """
        Block until signal ``name`` matches ``condition``. Returns False on
        timeout or if ``cancel`` is set.
        """
        self._spec(name)
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if matches(self.get(name), condition):
                return True
            if cancel is not None and cancel.is_set():
                return False
            if deadline is not None and time.time() > deadline:
                return False
            time.sleep(min(self.poll_s, 0.1))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            sigs = [
                {
                    "name": n,
                    "source": self._route[n],
                    "value": self._values.get(n),
                    "age_s": None if n not in self._stamps else round(now - self._stamps[n], 1),
                    "output": bool(s.get("output")),
                    "idle": s.get("idle"),
                    "description": s.get("description", ""),
                }
                for n, s in self.signals.items()
            ]
        return {
            "simulated": self.simulated,
            "backends": {n: {"connected": b.connected, "error": b.error} for n, b in self.backends.items()},
            "signals": sigs,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }

    @property
    def connected(self) -> bool:
        return bool(self.backends) and all(b.connected for b in self.backends.values())
