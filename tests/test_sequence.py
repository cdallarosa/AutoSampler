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
        {"type": "samples", "slot": "bottles", "wells": "A1-A2", "dwell_s": 0.1,
         "end_position": "park"}]}, deck)
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


def test_stop_freezes_in_place(homed, deck):
    """STOP halts and nothing moves afterwards - the needle stays where it was."""
    seq = Sequence.from_dict({"steps": [
        {"type": "move_to_well", "slot": "bottles", "well": "B2"},
        {"type": "lower"},
        {"type": "dwell", "seconds": 30}]}, deck)
    runner = SequenceRunner(homed, deck)
    runner.start(seq)
    assert wait_for(lambda: runner.index == 2)
    z_down = homed.get_position().z
    assert z_down > deck.slot("bottles").labware.z_top_mm  # needle is down in the bottle
    runner.abort()
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.ABORTED
    assert runner.stopped_index == 2 and runner.needle_down
    time.sleep(0.3)
    assert homed.get_position().z == pytest.approx(z_down, abs=0.2)


def _two_bottles(deck, dwell=0.5):
    return Sequence.from_dict({"name": "two", "steps": [
        {"type": "samples", "slot": "bottles", "wells": ["A1", "A2"], "dwell_s": dwell,
         "end_position": "park"}]}, deck)


def test_end_finishes_current_bottle_then_parks(homed, deck):
    runner = SequenceRunner(homed, deck)
    runner.start(_two_bottles(deck, dwell=0.6))
    assert wait_for(lambda: runner.needle_down)  # needle in A1
    runner.end()
    assert runner.end_pending
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.ENDED
    describes = [s.describe() for s in runner.steps[:runner.index + 1]]
    assert not any("A2" in d for d in describes), "should not start the next bottle"
    pos = homed.get_position()
    assert (pos.x, pos.y, pos.z) == pytest.approx(deck.position("park"), abs=0.2)


def test_end_while_needle_up_parks_immediately(homed, deck):
    seq = Sequence.from_dict({"steps": [{"type": "pause", "message": "hold"},
                                        {"type": "move_to_well", "slot": "bottles", "well": "F5"}]}, deck)
    runner = SequenceRunner(homed, deck)
    runner.start(seq)
    assert wait_for(lambda: runner.state == RunnerState.PAUSED)
    runner.end()
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.ENDED
    assert runner.index == 0  # never moved to F5


def test_resume_from_restarts_at_bottle(deck):
    from akta_autosampler.sequence import resume_from
    seq = _two_bottles(deck)
    a2 = next(i for i, s in enumerate(seq.steps) if s.type == "move_to_well" and "A2" in s.describe())
    resumed = resume_from(seq, a2 + 2)  # stopped while lowering into A2
    assert resumed.steps[0].type == "move_to_well" and "A2" in resumed.steps[0].describe()


def test_lower_without_well_fails(homed, deck):
    runner = SequenceRunner(homed, deck)
    runner.start(Sequence.from_dict({"steps": [{"type": "lower"}]}, deck))
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.FAILED
    assert "move to a bottle" in runner.message


def test_move_to_well_can_keep_the_needle_up(homed, deck):
    seq = Sequence.from_dict({"steps": [
        {"type": "move_to_well", "slot": "bottles", "well": "C3", "height": "safe"}]}, deck)
    runner = SequenceRunner(homed, deck)
    assert runner.start(seq)
    assert wait_for(lambda: not runner.is_active, 60)
    assert runner.state == RunnerState.COMPLETED, runner.message
    x, y, _ = deck.well_xyz("bottles", "C3", "top")
    pos = homed.get_position()
    assert (pos.x, pos.y, pos.z) == pytest.approx((x, y, homed.config.z_safe_mm), abs=0.2)


def test_tour_method_visits_every_position_once(deck):
    seq = Sequence.load(CONFIG_DIR / "sequences" / "tour_all_positions.json", deck)
    visits = [(s.slot, s.well) for s in seq.steps if s.type == "move_to_well"]
    expected = {("bottles", w) for w in deck.slot("bottles").labware.expand_wells("A1-F9")} | {("sample", "A1")}
    assert len(visits) == len(expected) == len(set(visits)) and set(visits) == expected
    assert all(s.height == "safe" for s in seq.steps if s.type == "move_to_well")
    assert visits != sorted(visits)  # shuffled, not A1, A2, ...
