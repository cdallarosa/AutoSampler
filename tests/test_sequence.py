import time

import pytest

from akta_autosampler.config import CONFIG_DIR
from akta_autosampler.sequence import RunnerState, Sequence, SequenceRunner


def wait_for(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_example_sequence_expands(deck):
    seq = Sequence.load(CONFIG_DIR / "sequences" / "example.json", deck)
    types = [s.type for s in seq.steps]
    assert types[0] == "home"
    assert types.count("move_to_well") == 3
    assert types[-1] == "move_to_position"


def test_unknown_step_rejected(deck):
    with pytest.raises(ValueError, match="Step 1"):
        Sequence.from_dict({"steps": [{"type": "aspirate"}]}, deck)


def test_runner_completes(homed, deck):
    seq = Sequence.from_dict({"name": "t", "steps": [
        {"type": "samples", "slot": "rack1", "wells": "A1-A2", "dwell_s": 0.1,
         "wash": {"position": "wash"}, "end_position": "park"}]}, deck)
    runner = SequenceRunner(homed, deck)
    assert runner.start(seq)
    assert wait_for(lambda: not runner.is_active, 60)
    assert runner.state == RunnerState.COMPLETED, runner.message
    pos = homed.get_position()
    assert (pos.x, pos.y, pos.z) == pytest.approx(deck.position("park"), abs=0.2)


def test_pause_step_waits_for_resume(homed, deck):
    seq = Sequence.from_dict({"steps": [{"type": "pause", "message": "go"}, {"type": "raise"}]}, deck)
    runner = SequenceRunner(homed, deck)
    runner.start(seq)
    assert wait_for(lambda: runner.state == RunnerState.PAUSED)
    assert runner.message == "go"
    time.sleep(0.2)
    assert runner.state == RunnerState.PAUSED
    runner.resume()
    assert wait_for(lambda: runner.state == RunnerState.COMPLETED)


def test_abort_raises_needle(homed, deck):
    seq = Sequence.from_dict({"steps": [
        {"type": "move_to_well", "slot": "rack1", "well": "B2"},
        {"type": "lower"},
        {"type": "dwell", "seconds": 30}]}, deck)
    runner = SequenceRunner(homed, deck)
    runner.start(seq)
    assert wait_for(lambda: runner.index == 2)
    assert homed.get_position().z > 100
    runner.abort()
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.ABORTED
    assert homed.get_position().z == pytest.approx(homed.config.z_safe_mm, abs=0.2)


def test_lower_without_well_fails(homed, deck):
    runner = SequenceRunner(homed, deck)
    runner.start(Sequence.from_dict({"steps": [{"type": "lower"}]}, deck))
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.FAILED
    assert "move_to_well" in runner.message
