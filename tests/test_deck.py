import pytest


def test_plate_corner_wells(deck):
    slot = deck.slot("bottles")
    ox, oy = slot.origin_mm
    ax, ay = slot.labware.a1_offset_mm
    p = 69.85  # 2.75 in pitch for 2.5 in bottles
    assert deck.well_xy("bottles", "A1") == pytest.approx((ox + ax, oy + ay))
    assert deck.well_xy("bottles", "F9") == pytest.approx((ox + ax + 8 * p, oy + ay + 5 * p))


def test_bottles_do_not_overlap_and_fit_workspace(deck, config):
    xy = [deck.well_xy(s.name, w) for s in deck.slots.values() for w in s.labware.wells()]
    r = 63.5 / 2  # 2.5 in bottles
    assert deck.well_xy("bottles", "A1") == (0.0, 0.0)  # A1 sits at home
    for i, (x1, y1) in enumerate(xy):
        assert 0 <= x1 <= config.axes["x"].max_mm  # the needle reaches every bottle centre
        assert 0 <= y1 <= config.axes["y"].max_mm
        for x2, y2 in xy[i + 1:]:
            assert ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 >= 2 * r + 6  # >= 0.25 in gap


def test_well_z_depths(deck):
    lw = deck.slot("bottles").labware
    assert deck.well_xyz("bottles", "B2", "top")[2] == lw.z_top_mm
    assert deck.well_xyz("bottles", "B2", "sample")[2] == lw.z_sample_mm


def test_invalid_wells(deck):
    with pytest.raises(ValueError):
        deck.well_xy("bottles", "G1")  # 6 rows
    with pytest.raises(ValueError):
        deck.well_xy("bottles", "A10")  # 9 columns
    with pytest.raises(ValueError):
        deck.well_xy("nope", "A1")


def test_expand_wells(deck):
    lw = deck.slot("bottles").labware
    assert lw.expand_wells("A8-B2, D5") == ["A8", "A9", "B1", "B2", "D5"]
    assert lw.expand_wells("a1") == ["A1"]


def test_all_wells_inside_gantry_limits(deck, config):
    for slot in deck.slots.values():
        for well in slot.labware.wells():
            x, y, z = deck.well_xyz(slot.name, well, "sample")
            assert config.axes["x"].min_mm <= x <= config.axes["x"].max_mm
            assert config.axes["y"].min_mm <= y <= config.axes["y"].max_mm
            assert config.axes["z"].min_mm <= z <= config.axes["z"].max_mm


def test_teach_a1(deck):
    deck.teach_a1("bottles", 80.0, 180.0)
    assert deck.well_xy("bottles", "A1") == pytest.approx((80.0, 180.0))


def test_single_sample_position(deck):
    assert list(deck.slot("sample").labware.wells()) == ["A1"]
