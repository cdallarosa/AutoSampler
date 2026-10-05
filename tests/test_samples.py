import time

import pytest

from akta_autosampler.samples import BottleRegistry, QueueEntry, SampleQueue, SAMPLE_LIST_DIR
from akta_autosampler.sequence import RunnerState, SequenceRunner


def test_insert_washes_between_samples():
    q = SampleQueue("t")
    for w in ("A1", "A2", "A3"):
        q.add("sample", "bottles", w, dwell_s=1)
    q.insert_washes("bottles", "F5", dwell_s=2)
    assert [(e.kind, e.well) for e in q.entries] == [
        ("sample", "A1"), ("wash", "F5"), ("sample", "A2"), ("wash", "F5"), ("sample", "A3")]
    q.insert_washes("bottles", "F5")  # idempotent: no double washes
    assert len(q.entries) == 5
    q.insert_washes("bottles", "F5", after_last=True)
    assert q.entries[-1].kind == "wash"
    q.remove_washes()
    assert [e.well for e in q.entries] == ["A1", "A2", "A3"]


def test_move_and_remove():
    q = SampleQueue()
    for w in ("A1", "A2", "A3"):
        q.add("sample", "bottles", w)
    q.move(2, -1)
    assert [e.well for e in q.entries] == ["A1", "A3", "A2"]
    q.move(0, -1)  # out of range: no-op
    q.remove(0)
    assert [e.well for e in q.entries] == ["A3", "A2"]


def test_bad_kind_rejected():
    with pytest.raises(ValueError):
        QueueEntry("aspirate", "bottles", "A1")


def test_registry_roles_and_save(deck, tmp_path):
    reg = BottleRegistry(deck, tmp_path / "samples.json")
    reg.update("bottles", "B2", name="Lot 42", role="sample")
    reg.update("bottles", "F4", role="wash")
    reg.save()
    reg2 = BottleRegistry(deck, tmp_path / "samples.json")
    assert reg2.get("bottles", "B2").name == "Lot 42"
    assert [b.well for b in reg2.with_role("wash")] == ["F4"]
    with pytest.raises(ValueError):
        reg.update("bottles", "A1", role="waste")


def test_default_config_has_wash_bottle(deck):
    assert BottleRegistry(deck).with_role("wash"), "config/samples.json should define a wash bottle"


def test_example_list_order_and_sequence(deck):
    q = SampleQueue.load(SAMPLE_LIST_DIR / "example_with_washes.json")
    seq = q.to_sequence(deck, registry=BottleRegistry(deck))
    moves = [s.describe() for s in seq.steps if s.type == "move_to_well"]
    assert moves[0] == "Move to A1 - Sample 1"
    assert "Wash" in moves[1] and "F5" in moves[1]
    assert "Sample 2" in moves[2]
    assert seq.steps[0].type == "home" and seq.steps[-1].type == "move_to_position"


def test_empty_list_rejected(deck):
    with pytest.raises(ValueError, match="empty"):
        SampleQueue().to_sequence(deck)


def test_save_load_roundtrip(tmp_path):
    q = SampleQueue("rt")
    q.add("sample", "sample", "A1", dwell_s=3, handshake=True)
    q.add("wash", "bottles", "F5")
    q.save(tmp_path / "q.json")
    q2 = SampleQueue.load(tmp_path / "q.json")
    assert q2.to_dict() == q.to_dict()


def test_queue_runs_in_order(homed, deck):
    q = SampleQueue("run")
    q.add("sample", "bottles", "A1", dwell_s=0.1)
    q.add("wash", "bottles", "F5", dwell_s=0.1)
    q.add("sample", "bottles", "A2", dwell_s=0.1)
    seq = q.to_sequence(deck, home_first=False)
    runner = SequenceRunner(homed, deck)
    visited = []
    orig = homed.safe_move_to

    def spy(x=None, y=None, z=None, speed=None):
        visited.append((round(-1 if x is None else x, 1), round(-1 if y is None else y, 1)))
        return orig(x, y, z, speed)
    homed.safe_move_to = spy
    runner.start(seq)
    deadline = time.time() + 60
    while runner.is_active and time.time() < deadline:
        time.sleep(0.05)
    assert runner.state == RunnerState.COMPLETED, runner.message
    wells = [deck.well_xy("bottles", w) for w in ("A1", "F5", "A2")]
    assert visited[:3] == [(round(x, 1), round(y, 1)) for x, y in wells]


def test_quick_build_row_and_column_with_wash_and_blank(deck, tmp_path):
    from akta_autosampler.samples import BottleRegistry, SampleQueue
    reg = BottleRegistry(deck, tmp_path / "none.json")
    reg.update("bottles", "F5", role="wash")
    reg.update("bottles", "F9", role="blank")
    reg.update("bottles", "A2", role="empty")
    es = SampleQueue.build(reg, 3, first="A1", order="row", sample_dwell_s=5, handshake=True,
                           wash=("bottles", "F5"), wash_dwell_s=10, blank=("bottles", "F9"))
    assert [(e.kind, e.well) for e in es] == [("sample", "A1"), ("wash", "F5"), ("blank", "F9"),
                                              ("sample", "A3"), ("wash", "F5"), ("blank", "F9"), ("sample", "A4")]
    assert all(e.handshake == (e.kind == "sample") for e in es) and es[1].dwell_s == 10
    col = SampleQueue.build(reg, 3, first="A1", order="column", wash=("bottles", "F5"), wash_after_last=True)
    assert [e.well for e in col if e.kind == "sample"] == ["A1", "B1", "C1"] and col[-1].kind == "wash"
    with pytest.raises(ValueError):
        SampleQueue.build(reg, 99)


def test_auto_wash_goes_between_samples_as_they_are_added():
    q = SampleQueue("auto")
    for w in ("A1", "A2", "A3"):
        q.add_sample("bottles", w, wash=("bottles", "F5"), wash_dwell_s=8, dwell_s=3)
    assert [(e.kind, e.well) for e in q.entries] == [("sample", "A1"), ("wash", "F5"), ("sample", "A2"),
                                                     ("wash", "F5"), ("sample", "A3")]
    assert q.entries[1].dwell_s == 8 and q.entries[2].dwell_s == 3
    q.add("wash", "bottles", "F5")                 # a manual wash: the next sample doesn't get a second one
    q.add_sample("bottles", "A4", wash=("bottles", "F5"))
    assert [e.kind for e in q.entries[-3:]] == ["sample", "wash", "sample"]
